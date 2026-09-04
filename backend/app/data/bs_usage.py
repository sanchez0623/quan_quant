# -*- coding: utf-8 -*-
"""baostock API 用量监控与限流（数据管理·API调用监控）。

产品需求（baostock 访问限制）：
- 每日 API 请求 <= 50000，超过后进入黑名单控制；且不能并发连接访问。
- 本自然年度初次被黑名单限制：冻结 6 小时后自动解除；本年多次被限制时长自动增长。
- 限制时长 = 本年累计限制次数 × 6 小时。
- 待释放时间为空时，提示"等待 5 分钟后刷新"。

设计要点（任务池是 3 个独立进程，各自有独立 baostock 会话，必须跨进程一致）：
- 日用量计数：SQLite 表 bs_usage 原子 upsert（WAL，跨进程安全）。
- 禁止并发连接：全局文件锁（msvcrt/fcntl）串行化每次 baostock 查询。
- 黑名单状态：SQLite 表 bs_blacklist 记录 IP / 今年累计次数 / 预计释放时间；
  由 sources._ensure_login 在登录/查询返回错误码 10001011 时写入。
- 达上限行为：拒绝并抛出 BsDailyCapExceeded（任务以明确错误失败，不硬撞黑名单）。
"""
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from .. import config, db

# 每日 API 上限（baostock 官方限制）；可用环境变量覆盖
DAILY_CAP = int(os.environ.get("BS_DAILY_CAP", "50000"))
# 每次被限制的冻结时长基数（小时）
FREEZE_HOURS_PER_HIT = 6
# baostock 黑名单错误码
BLACKLIST_CODE = "10001011"
# 跨进程串行锁文件（放数据目录，避免与主库写锁互相影响）
_LOCK_FILE = os.path.join(str(config.DATA_DIR), ".bs.lock")
# 可选：查询间隔节流（秒），默认 0=不节流
_MIN_INTERVAL = float(os.environ.get("BS_MIN_INTERVAL", "0"))

# 公网 IP echo 端点（轮询，返回纯 IPv4 或含 IP 文本均可；国内可达优先）
_IP_ECHO_URLS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ip.3322.net",
    "https://4.ipw.cn",
    "https://myip.ipip.net",
]
_IP_CACHE: dict = {"ip": "", "ts": 0.0}
_IP_CACHE_TTL = 600  # 公网 IP 很少变，缓存 10 分钟

_IPV4_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")

# 官方黑名单账本接口（用户实测：POST {"ip": "..."} -> stats.total / data[].date
# / releaseDate / yearlyRestrictCount；data 按时间倒序）
_BLACKLIST_STATS_URL = "https://www.baostock.com/helpdocs/api/wd-blacklist-stats"


def _fetch_official_blacklist(ip: str) -> dict | None:
    """拉取 baostock 官方黑名单账本。返回
    {"total": 今年限制次数, "latest_date": 最新事件日期(YYYY-MM-DD),
     "latest_release": 官方释放时间原文}；接口不可达/结构变化返回 None。"""
    if not ip:
        return None
    try:
        import httpx
        with httpx.Client(timeout=5, trust_env=False) as client:
            r = client.post(_BLACKLIST_STATS_URL, json={"ip": ip})
            if r.status_code != 200:
                return None
            resp = r.json()
        stats = resp.get("stats") or {}
        data = stats.get("data") or []
        total = int(stats.get("total") or resp.get("yearlyRestrictCount")
                    or len(data) or 0)
        latest = data[0] if data else {}
        return {"total": total,
                "latest_date": str(latest.get("date") or "")[:10],
                "latest_release": str(latest.get("releaseDate") or "")}
    except Exception:
        return None


def _clean_release(raw: str) -> str | None:
    """官方 releaseDate（如 2026-09-03 23:10:00.0）-> 可入库/可解析的时间串"""
    s = str(raw or "").strip().replace("T", " ")
    if not s:
        return None
    return s.split(".")[0]


def _extract_ipv4(text: str) -> str:
    """从任意文本中提取合法的 IPv4（兼容纯 IP 与中文包装格式）。"""
    m = _IPV4_RE.search(text or "")
    if not m:
        return ""
    parts = m.group(1).split(".")
    if all(0 <= int(p) <= 255 for p in parts):
        return m.group(1)
    return ""


class BsDailyCapExceeded(RuntimeError):
    """今日 baostock 用量已达上限，拒绝新请求"""


class BsBlacklisted(RuntimeError):
    """IP 已被 baostock 黑名单限制（含预计解除时间）"""


class BsLockTimeout(RuntimeError):
    """等待 baostock 全局串行锁超时"""


class BsUsageTracker:
    """baostock 用量监控（跨进程）：计数 / 串行锁 / 黑名单 / 出口IP"""

    def __init__(self):
        # 仅本进程内的活跃请求数（跨进程并发由文件锁保证 <=1）
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._last_call_ts = 0.0

    # ---- 日用量计数（跨进程） ----
    def daily_count(self, today: str | None = None) -> int:
        today = today or datetime.now().strftime("%Y-%m-%d")
        with db.conn() as c:
            row = c.execute("SELECT count FROM bs_usage WHERE date=?", (today,)).fetchone()
            return int(row[0]) if row else 0

    def record_call(self) -> int:
        """原子 +1，返回今日最新计数。"""
        today = datetime.now().strftime("%Y-%m-%d")
        with db.conn() as c:
            c.execute(
                "INSERT INTO bs_usage(date,count) VALUES(?,1) "
                "ON CONFLICT(date) DO UPDATE SET count=count+1",
                (today,),
            )
            row = c.execute("SELECT count FROM bs_usage WHERE date=?", (today,)).fetchone()
            return int(row[0]) if row else 1

    # ---- 跨进程串行锁（禁止并发连接） ----
    @contextmanager
    def serialize(self, timeout: float = 30.0):
        """包住一次 baostock 查询：全局同一时刻仅 1 个连接。"""
        fd = self._acquire_fd(timeout)
        with self._inflight_lock:
            self._inflight += 1
        try:
            # 可选查询间隔节流
            if _MIN_INTERVAL > 0:
                wait = _MIN_INTERVAL - (time.time() - self._last_call_ts)
                if wait > 0:
                    time.sleep(wait)
            yield
            self._last_call_ts = time.time()
        finally:
            with self._inflight_lock:
                self._inflight = max(0, self._inflight - 1)
            self._release_fd(fd)

    def in_flight(self) -> int:
        with self._inflight_lock:
            return self._inflight

    def _acquire_fd(self, timeout: float) -> int:
        os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR)
        deadline = time.time() + timeout
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return fd
                except OSError:
                    if time.time() > deadline:
                        os.close(fd)
                        raise BsLockTimeout("等待 baostock 全局串行锁超时") from None
                    time.sleep(0.05)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    if time.time() > deadline:
                        os.close(fd)
                        raise BsLockTimeout("等待 baostock 全局串行锁超时") from None
                    time.sleep(0.05)

    def _release_fd(self, fd: int) -> None:
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                os.close(fd)
            except Exception:
                pass

    # ---- 黑名单状态（跨进程） ----
    def _blacklist_row(self) -> dict | None:
        with db.conn() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM bs_blacklist ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def is_blacklisted(self) -> bool:
        row = self._blacklist_row()
        if not row or not row.get("release_at"):
            return False
        try:
            return datetime.now() < datetime.fromisoformat(row["release_at"])
        except ValueError:
            return False

    def last_blacklist(self) -> dict | None:
        """最近一次有释放时间的黑名单记录（供报错信息用）。"""
        with db.conn() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM bs_blacklist WHERE release_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def record_blacklist(self, ip: str = "") -> dict:
        """检测到被限制（错误码 10001011）：优先以 baostock 官方账本对账。

        官方接口 wd-blacklist-stats 是唯一事实源：
        - 官方最新记录日期 == 本地最近一条检测日 => 同一次限制事件：
          不累加计数，仅用官方 releaseDate 校准本地释放时间
          （本地按"次数×6h"估算的释放期会早于/晚于官方，提前判定
          "已解除"会把限制期内后续被拒误判为新事件而虚增计数）。
        - 新事件：freeze_count 直接采用官方 yearlyRestrictCount。
        - 官方接口不可达：回退本地估算（同限制期内重复探测不累加）。
        每次黑名单事件才调用，频率极低。"""
        now = datetime.now()
        row = self._blacklist_row()
        official = _fetch_official_blacklist(ip or self.public_ip())
        if official is not None:
            latest_local_day = (str(row.get("detected_at") or "")[:10]
                                if row else "")
            same_event = bool(official["latest_date"]) and \
                official["latest_date"] == latest_local_day
            release = _clean_release(official["latest_release"])
            with db.conn() as c:
                if same_event and row:
                    c.execute(
                        "UPDATE bs_blacklist SET freeze_count=?, release_at=?, "
                        "last_check=? WHERE id=?",
                        (official["total"], release,
                         now.isoformat(timespec="seconds"), row["id"]))
                    return {"ip": row.get("ip") or ip,
                            "freeze_count": official["total"],
                            "detected_at": row["detected_at"],
                            "release_at": release}
                n = official["total"] or (
                    (int(row["freeze_count"]) + 1) if row else 1)
                detected = (official["latest_date"]
                            or now.isoformat(timespec="seconds"))
                c.execute(
                    "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,"
                    "release_at,last_check) VALUES(?,?,?,?,?)",
                    (ip or "", n, detected, release,
                     now.isoformat(timespec="seconds")))
                return {"ip": ip or "", "freeze_count": n,
                        "detected_at": detected, "release_at": release}
        # ---- 官方不可达：本地估算兜底 ----
        active = False
        if row and row.get("release_at"):
            try:
                active = now < datetime.fromisoformat(row["release_at"])
            except ValueError:
                active = False
        if active:
            with db.conn() as c:
                c.execute("UPDATE bs_blacklist SET last_check=? WHERE id=?",
                          (now.isoformat(timespec="seconds"), row["id"]))
            return {"ip": row.get("ip") or ip, "freeze_count": int(row["freeze_count"]),
                    "detected_at": row["detected_at"], "release_at": row["release_at"]}
        n = (int(row["freeze_count"]) + 1) if row else 1
        release_at = now + timedelta(hours=n * FREEZE_HOURS_PER_HIT)
        with db.conn() as c:
            c.execute(
                "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,release_at,last_check) "
                "VALUES(?,?,?,?,?)",
                (ip or "", n, now.isoformat(timespec="seconds"),
                 release_at.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
        return {"ip": ip or "", "freeze_count": n,
                "detected_at": now.isoformat(timespec="seconds"),
                "release_at": release_at.isoformat(timespec="seconds")}

    def mark_released(self) -> None:
        """登录成功说明当前请求可通：仅刷新最近黑名单记录的检查时间。

        不改写 release_at——服务端 IP 冻结不受客户端单次登录影响，
        提前抹掉释放时间会把限制期内后续被拒误判为新事件而虚增计数
        （实测：2026-09-03 20:18 登录成功改写释放期后，21:55 的拒绝
        被误记为第 3 次限制，官方账本实际仅 2 次）。"""
        now = datetime.now().isoformat(timespec="seconds")
        with db.conn() as c:
            c.execute(
                "UPDATE bs_blacklist SET last_check=? "
                "WHERE id=(SELECT MAX(id) FROM bs_blacklist)",
                (now,),
            )

    def touch_check(self) -> None:
        """更新最近检查时间（health_check 主动探测后调用）。"""
        with db.conn() as c:
            c.execute(
                "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,release_at,last_check) "
                "VALUES('',0,NULL,NULL,?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )

    # ---- 公网 IP ----
    def public_ip(self) -> str:
        """取公网 IP（baostock 服务端视角）：
        1) BS_MONITOR_IP 环境变量覆盖；2) 公网 IP echo 服务（多端点轮询，短超时）；
        3) 全部失败回退本机出口接口 IP（NAT 下可能为内网 IP）。结果内存缓存 10 分钟。"""
        override = os.environ.get("BS_MONITOR_IP")
        if override:
            return override
        now = time.time()
        if _IP_CACHE["ip"] and now - _IP_CACHE["ts"] < _IP_CACHE_TTL:
            return _IP_CACHE["ip"]
        for url in _IP_ECHO_URLS:
            ip = self._fetch_public_ip(url)
            if ip:
                _IP_CACHE["ip"] = ip
                _IP_CACHE["ts"] = now
                return ip
        return self._outbound_interface_ip()

    @staticmethod
    def _fetch_public_ip(url: str) -> str:
        try:
            import httpx
            with httpx.Client(timeout=3, trust_env=False) as client:
                r = client.get(url)
                if r.status_code == 200:
                    return _extract_ipv4(r.text)
        except Exception:
            pass
        return ""

    @staticmethod
    def _outbound_interface_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))  # UDP connect 不发包，仅取本机出口接口 IP
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return ""

    # ---- 监控快照（API 用） ----
    def get_monitor(self) -> dict:
        row = self._blacklist_row() or {}
        now = datetime.now()
        freeze_count = int(row.get("freeze_count") or 0)
        release_at = row.get("release_at")
        blacklisted = False
        if release_at:
            try:
                blacklisted = now < datetime.fromisoformat(release_at)
            except ValueError:
                blacklisted = False
        return {
            "ip": self.public_ip(),
            "today_count": self.daily_count(),
            "cap": DAILY_CAP,
            "concurrency": self.in_flight(),
            "blacklisted": blacklisted,
            "freeze_count": freeze_count,
            "release_at": release_at,
            "last_check": row.get("last_check"),
            "hint": "公网 IP（baostock 服务端视角，经 IP echo 服务探测，可设 BS_MONITOR_IP 覆盖）；"
                    "待释放时间为空时请等待 5 分钟后刷新",
        }


tracker = BsUsageTracker()
