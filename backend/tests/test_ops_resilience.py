# -*- coding: utf-8 -*-
"""运维弹性（OPS_RESILIENCE）：
A1 run_task 捕获 BaseException（SystemExit/KeyboardInterrupt 静默死亡根因）
A2 黑名单报错只认活跃记录
B1 evening 失败自动重试（30 分钟间隔、每晚 2 次上限）
B2 日线断点续传跳过已最新票
D1 调度类任务失败推飞书
"""
import datetime as dt
from datetime import datetime, timedelta

# ---------------- A1: run_task 捕获 BaseException ----------------

def test_run_task_captures_base_exception():
    """SystemExit 穿透 except Exception 曾致 worker 静默死亡（error 仅 10 字符）；
    现应就地转 failed 并记录异常类型"""
    from app import db, task_manager

    tid = "baseexc_test_001"
    db.create_task(tid, "BaseException测试", "backtest", payload={})
    orig = task_manager._TASK_FUNCS.get("backtest")

    def _boom(**kwargs):
        raise SystemExit(1)

    task_manager._TASK_FUNCS["backtest"] = _boom
    try:
        task_manager.run_task("backtest", {"task_id": tid, "db_path": None})
    finally:
        task_manager._TASK_FUNCS["backtest"] = orig
    t = db.get_task(tid)
    assert t["status"] == "failed"
    assert "SystemExit" in (t["error"] or ""), f"error 应含异常类型: {t['error']}"


# ---------------- A2: 黑名单报错只认活跃记录 ----------------

def test_last_blacklist_only_active():
    """过释放期的旧记录不能作为登录失败的报错依据（09-16 误报事故）"""
    from app import db
    from app.data.bs_usage import tracker

    now = datetime.now()
    with db.conn() as c:
        c.execute("DELETE FROM bs_blacklist")
        c.execute(
            "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,release_at,last_check) "
            "VALUES(?,?,?,?,?)",
            ("1.2.3.4", 2, now.isoformat(timespec="seconds"),
             (now - timedelta(hours=1)).isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds")))
    try:
        assert tracker.last_blacklist() is None, "已过释放期的记录应返回 None"
        with db.conn() as c:
            c.execute("UPDATE bs_blacklist SET release_at=?",
                      ((now + timedelta(hours=1)).isoformat(timespec="seconds"),))
        row = tracker.last_blacklist()
        assert row is not None and row["ip"] == "1.2.3.4", "限制期内的记录应返回"
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM bs_blacklist")


# ---------------- B1: evening 失败自动重试 ----------------

def test_scheduler_evening_retry(monkeypatch):
    """失败后隔 30 分钟重试一次；第 2 次失败后不再重试且放行分钟线"""
    from app.live import scheduler

    submitted: list[tuple] = []
    monkeypatch.setattr(scheduler.manager, "submit",
                        lambda kind, tid, **kw: submitted.append((kind, tid)))
    monkeypatch.setattr(scheduler, "_is_trading_day", lambda t, n: True)
    day = "2026-09-10"
    scheduler.db.set_meta("auto_postclose_date", day)  # 隔离：屏蔽 postclose 顺带提交

    # 首次提交
    r1 = scheduler.tick(dt.datetime(2026, 9, 10, 18, 20))
    assert r1["submitted"] == ["evening"]
    assert scheduler._evening_attempts(day) == 1
    # 任务 pending（真实库状态）：不重试
    assert scheduler.tick(dt.datetime(2026, 9, 10, 18, 40))["submitted"] == []
    # 失败后 10 分钟：未到 30 分钟间隔，不重试、分钟线不抢跑
    monkeypatch.setattr(scheduler.db, "get_task",
                        lambda tid, **kw: {"status": "failed",
                                           "finished_at": f"{day} 18:50:00"})
    assert scheduler.tick(dt.datetime(2026, 9, 10, 19, 0))["submitted"] == []
    # 失败后 31 分钟：触发重试（第 2 次）
    r2 = scheduler.tick(dt.datetime(2026, 9, 10, 19, 21))
    assert r2["submitted"] == ["evening"]
    assert scheduler._evening_attempts(day) == 2
    # 第 2 次失败：evening 次数用尽不再重试，分钟线放行（任务隔离）
    r3 = scheduler.tick(dt.datetime(2026, 9, 10, 19, 22))
    assert r3["submitted"] == ["minute5"]
    # minute5 当日幂等
    assert scheduler.tick(dt.datetime(2026, 9, 10, 19, 30))["submitted"] == []


# ---------------- B2: 日线断点续传 ----------------

def test_effective_window_end():
    """哨兵 end_date 用日历最后交易日；显式末日直用；无日历禁用跳过"""
    from app.data.updater import _effective_window_end

    cal = ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert _effective_window_end("2099-12-31", cal) == "2026-01-07"
    assert _effective_window_end("2026-01-06", cal) == "2026-01-06"
    assert _effective_window_end("2099-12-31", []) == ""


def test_daily_max_dates(demo_env):
    """code -> 库内最大日期映射（跳过判定数据源）"""
    from app.data import store

    data_dir, start, end = demo_env
    m = store.daily_max_dates(data_dir)
    assert m, "演示数据应产出映射"
    assert all(start <= v <= end for v in m.values())


# ---------------- D1: 调度任务失败推飞书 ----------------

def test_notify_failed_pushes_feishu(monkeypatch):
    """failed 的调度类任务推送；success 与非调度类型（backtest）不推"""
    from app import db
    from app.task_manager import TaskManager

    pushed: list[str] = []
    monkeypatch.setattr("app.live.feishu.send_text",
                        lambda text: pushed.append(text) or True)
    mgr = TaskManager()

    tid = "notify_fail_001"
    db.create_task(tid, "定时失败推送测试", "data_update", payload={"scope": "daily"})
    db.finish_task(tid, "failed", error="boom")
    mgr._notify_failed(tid)
    assert pushed and "定时任务失败" in pushed[0]
    assert "data_update" in pushed[0]

    n = len(pushed)
    tid2 = "notify_ok_001"
    db.create_task(tid2, "成功任务", "data_update", payload={})
    db.finish_task(tid2, "success", payload={})
    mgr._notify_failed(tid2)
    assert len(pushed) == n, "success 不推送"

    tid3 = "notify_bt_001"
    db.create_task(tid3, "回测失败", "backtest", payload={})
    db.finish_task(tid3, "failed", error="x")
    mgr._notify_failed(tid3)
    assert len(pushed) == n, "非调度类型不推送"
