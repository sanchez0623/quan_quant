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
from datetime import datetime

from .. import db
from ..data import store
from ..task_manager import manager
from . import premarket

TICK_SEC = 30
MORNING_WINDOW = (8 * 60 + 25, 11 * 60 + 30)     # 08:25~11:30
POSTCLOSE_WINDOW = (15 * 60 + 25, 23 * 60 + 59)  # 15:25~23:59

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
        db.create_task(task_id, name, "live_premarket",
                       payload={"update_data": True, "push": True,
                                "auto": True})
        manager.submit("live_premarket", task_id, update_data=True, push=True)
    else:
        db.create_task(task_id, name, "live_postclose",
                       payload={"push": True, "auto": True})
        manager.submit("live_postclose", task_id, push=True)
    db.set_meta(f"auto_{kind}_date", today)


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
