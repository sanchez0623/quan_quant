# -*- coding: utf-8 -*-
"""每日自动调度（LIVE_SIGNAL_SYSTEM §5 运行节奏落地）。

- 盘前 08:25~11:30 窗口：自动提交盘前编排任务（含日线增量更新+盘前流程）
- 盘后 15:25~23:59 窗口：自动提交盘后流程（分钟线落库+对账卡）
- 仅交易日（trade_calendar；缺当日记录按周一~五兜底）
- 当日幂等：sig_meta 记 auto_morning_date / auto_postclose_date，
  手动提交（POST /morning、/postclose）写同一标记 -> 手动+自动互斥
- 窗口宽松（开机晚也能补跑），提交成功即写标记，失败下个 tick 重试
- auto_schedule=False 时调度器空转（配置关闭）

线程模型：daemon 守护线程 30s tick，异常捕获打日志不死；
start() 幂等（模块级引用），由 main.py 启动时拉起。
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

from .. import db
from ..data import store
from ..task_manager import manager
from . import premarket

TICK_SEC = 30
MORNING_WINDOW = (8 * 60 + 25, 11 * 60 + 30)     # 08:25~11:30
POSTCLOSE_WINDOW = (15 * 60 + 25, 23 * 60 + 59)  # 15:25~23:59
EVENING_WINDOW = (18 * 60 + 10, 23 * 60 + 59)    # 18:10~23:59（baostock 当日日线 17:30 后就绪）
# 盘后失败重试（EVENING_RETRY）：每晚最多提交 2 次，失败后隔 30 分钟可重试
EVENING_MAX_ATTEMPTS = 2
EVENING_RETRY_GAP_SEC = 30 * 60

_thread: threading.Thread | None = None


def _is_trading_day(today: str, now: datetime) -> bool:
    try:
        cal = store.read_calendar()
        if cal is not None and cal.height:
            row = cal.filter(cal["date"] == today)
            if row.height:
                return bool(row["is_open"][0])
    except Exception:
        pass
    return now.weekday() < 5


def _in_window(now: datetime, window: tuple[int, int]) -> bool:
    m = now.hour * 60 + now.minute
    return window[0] <= m <= window[1]


def _submitted(kind: str, today: str) -> bool:
    return db.get_meta(f"auto_{kind}_date") == today


def _submit_task(kind: str, today: str, name: str) -> None:
    """提交任务成功后才写当日标记（失败下个 tick 重试）"""
    import uuid
    task_id = "live_" + uuid.uuid4().hex[:12]
    if kind == "morning":
        # 盘前只做信号流程（数据由盘后 18:10 evening 任务负责，避免盘前串行拉数小时）
        db.create_task(task_id, name, "live_premarket",
                       payload={"update_data": False, "push": True,
                                "auto": True})
        manager.submit("live_premarket", task_id, update_data=False, push=True)
    elif kind == "evening":
        latest = store.daily_latest_date()
        base = datetime.strptime(latest, "%Y-%m-%d") if latest else datetime.now()
        start = (base - timedelta(days=5)).strftime("%Y-%m-%d")
        db.create_task(task_id, name, "data_update",
                       payload={"scope": "daily", "start_date": start,
                                "end_date": "2099-12-31", "auto": True})
        manager.submit("data_update", task_id, scope="daily",
                       start_date=start, end_date="2099-12-31")
        # 记录日线任务 id：供 tick 轮询终态后错峰提交分钟线任务
        # （数据更新必须串行——baostock 并发连接触发黑名单）
        db.set_meta("auto_evening_daily_id", task_id)
        # 提交次数记账（EVENING_RETRY）：首提=1，失败重试=2，达上限当晚不再重试
        prev_date, _, prev_n = (db.get_meta("auto_evening_attempt") or "").partition("|")
        n = (int(prev_n) + 1) if (prev_date == today and prev_n) else 1
        db.set_meta("auto_evening_attempt", f"{today}|{n}")
    elif kind == "minute5":
        # 与日线同窗口的分钟线增量；独立任务与日线互相隔离（日线失败不影响）
        latest = store.daily_latest_date()
        base = datetime.strptime(latest, "%Y-%m-%d") if latest else datetime.now()
        start = (base - timedelta(days=5)).strftime("%Y-%m-%d")
        db.create_task(task_id, name, "data_update",
                       payload={"scope": "minute5", "start_date": start,
                                "end_date": "2099-12-31", "auto": True})
        manager.submit("data_update", task_id, scope="minute5",
                       start_date=start, end_date="2099-12-31")
    else:
        db.create_task(task_id, name, "live_postclose",
                       payload={"push": True, "auto": True})
        manager.submit("live_postclose", task_id, push=True)
    db.set_meta(f"auto_{kind}_date", today)


def _evening_attempts(today: str) -> int:
    """今日 evening 已提交次数（0=今日未提交过）"""
    d, _, n = (db.get_meta("auto_evening_attempt") or "").partition("|")
    return int(n) if (d == today and n) else 0


def _evening_due(today: str, now: datetime) -> bool:
    """evening 是否应提交（EVENING_RETRY）。

    首次：今日未提交过即在窗口内提交。重试：上一任务 failed/cancelled、
    次数未达上限、且距终态时间 >= 30 分钟（给分批落库/收尾留缓冲）。
    pending/running/cancelling/success 一律不再提交。"""
    if not _submitted("evening", today):
        return True
    tid = db.get_meta("auto_evening_daily_id")
    if not tid:
        return False
    t = db.get_task(tid)
    if t is None or t.get("status") in ("pending", "running", "cancelling",
                                        "success"):
        return False
    if _evening_attempts(today) >= EVENING_MAX_ATTEMPTS:
        return False
    try:
        fin = datetime.strptime(t.get("finished_at") or "",
                                "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (now - fin).total_seconds() >= EVENING_RETRY_GAP_SEC


def tick(now: datetime | None = None) -> dict:
    """一次调度检查（可测试）：返回本 tick 的动作。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    out = {"trading_day": False, "submitted": []}
    try:
        cfg = {**premarket.DEFAULT_CFG, **db.get_live_config()}
        if not cfg.get("auto_schedule", True):
            out["skipped"] = "auto_schedule=off"
            return out
        if not _is_trading_day(today, now):
            return out
        out["trading_day"] = True
        if _in_window(now, MORNING_WINDOW) and not _submitted("morning", today):
            _submit_task("morning", today, f"实盘盘前流程（自动）{today}")
            out["submitted"].append("morning")
        if _in_window(now, POSTCLOSE_WINDOW) and not _submitted("postclose", today):
            _submit_task("postclose", today, f"实盘盘后流程（自动）{today}")
            out["submitted"].append("postclose")
        # 盘后数据更新（18:10 起，baostock 当日日线就绪后）：串行全市场增量，
        # 供次日盘前信号直接使用（盘前不再耗时拉数）。失败自动重试（EVENING_RETRY）
        submitted_evening = False
        if _in_window(now, EVENING_WINDOW) and _evening_due(today, now):
            _submit_task("evening", today, f"实盘盘后数据更新（自动）{today}")
            out["submitted"].append("evening")
            submitted_evening = True
        # 分钟线跟随提交（不受窗口限制，晚间随时可触发）：当日日线任务达
        # 终态后错峰提交独立 minute5 任务——防 baostock 并发黑名单（必须
        # 串行）；日线失败不影响分钟线照跑（任务互相隔离），但需等 evening
        # 重试额度用尽（否则重试的日线与分钟线并发抢 baostock）；本 tick 刚
        # （重）提交过 evening 时也跳过，等新任务出终态
        if (not submitted_evening and _submitted("evening", today)
                and not _submitted("minute5", today)):
            daily_id = db.get_meta("auto_evening_daily_id")
            daily_task = db.get_task(daily_id) if daily_id else None
            if daily_task and daily_task.get("status") in ("success", "failed",
                                                           "cancelled"):
                done_retrying = (daily_task["status"] == "success"
                                 or _evening_attempts(today) >= EVENING_MAX_ATTEMPTS)
                if done_retrying:
                    _submit_task("minute5", today,
                                 f"实盘盘后分钟线更新（自动）{today}")
                    out["submitted"].append("minute5")
    except Exception:
        out["error"] = traceback.format_exc(limit=3)
    return out


def _loop() -> None:
    while True:
        try:
            r = tick()
            for k in r.get("submitted", []):
                print(f"[scheduler] auto-submitted: {k}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(TICK_SEC)


def start() -> None:
    """启动调度线程（幂等；daemon=True 随主进程退出）"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="live-scheduler", daemon=True)
    _thread.start()
