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
        """检测到被限制（错误码 10001011）：今年累计次数+1，冻结时长=次数×6h。"""
        now = datetime.now()
        row = self._blacklist_row()
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

    def touch_check(self) -> None:
        """更新最近检查时间（health_check 主动探测后调用）。"""
        with db.conn() as c:
            c.execute(
                "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,release_at,last_check) "
                "VALUES('',0,NULL,NULL,?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )

    # ---- 出口 IP ----
    def outbound_ip(self) -> str:
        override = os.environ.get("BS_MONITOR_IP")
        if override:
            return override
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))  # UDP connect 不发包，仅取本机出口 IP
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
            "ip": self.outbound_ip(),
            "today_count": self.daily_count(),
            "cap": DAILY_CAP,
            "concurrency": self.in_flight(),
            "blacklisted": blacklisted,
            "freeze_count": freeze_count,
            "release_at": release_at,
            "last_check": row.get("last_check"),
            "hint": "本机出口 IP（NAT 环境下可能为内网 IP，可用 BS_MONITOR_IP 覆盖）；"
                    "待释放时间为空时请等待 5 分钟后刷新",
        }


tracker = BsUsageTracker()
