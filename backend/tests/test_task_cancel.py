# -*- coding: utf-8 -*-
"""任务协作式取消测试：request_cancel 标记 / update_progress 检查点 /
run_task 落 cancelled 终态（含执行前取消）。"""
import pytest

from app import db, task_manager


@pytest.fixture(autouse=True)
def _tasks_env():
    db.init_db()
    yield
    with db.conn() as c:
        c.execute("DELETE FROM tasks")


def test_request_cancel_states():
    """running/pending -> cancelling；终态不动；不存在 -> not_found"""
    db.create_task("t1", "任务1", "data_update")
    assert db.request_cancel("t1") == "cancelling"
    assert db.get_task("t1")["status"] == "cancelling"
    db.finish_task("t1", "cancelled")
    assert db.request_cancel("t1") == "cancelled"      # 终态不再变更
    assert db.request_cancel("t404") == "not_found"


def test_update_progress_raises_on_cancelling():
    """进度检查点：cancelling -> 抛 TaskCancelled（子进程自杀信号）"""
    db.create_task("t2", "任务2", "data_update")
    db.update_progress("t2", 10.0, "step1")            # 正常写
    assert db.get_task("t2")["progress"] == 10.0
    db.request_cancel("t2")
    with pytest.raises(db.TaskCancelled):
        db.update_progress("t2", 20.0, "step2")
    assert db.get_task("t2")["progress"] == 10.0, "取消后的进度不应再写"
    assert db.get_task("t2")["status"] == "cancelling"


def test_run_task_cancels_at_checkpoint(monkeypatch):
    """任务函数在检查点抛 TaskCancelled -> run_task 落 cancelled 终态"""
    def fake_task(task_id: str, db_path: str):
        db.update_progress(task_id, 10.0, "step1", db_path)
        db.request_cancel(task_id, db_path=db_path)      # 用户此时点停止
        db.update_progress(task_id, 50.0, "step2", db_path)   # 检查点 -> 抛
        db.finish_task(task_id, "success", db_path=db_path)   # 不应到达

    monkeypatch.setitem(task_manager._TASK_FUNCS, "fake", fake_task)
    db.create_task("t3", "任务3", "fake")
    task_manager.run_task("fake", {"task_id": "t3", "db_path": db.DEFAULT_DB
                                   if hasattr(db, "DEFAULT_DB") else None})
    t = db.get_task("t3")
    assert t["status"] == "cancelled"
    assert t["progress"] == 10.0, "取消后不再推进"
    assert "取消" in (t["error"] or "")


def test_run_task_skips_cancelled_before_start(monkeypatch):
    """排队期间已被请求取消的任务：入口检查直接落 cancelled，不执行"""
    called = []

    def fake_task(task_id: str, db_path: str):
        called.append(task_id)

    monkeypatch.setitem(task_manager._TASK_FUNCS, "fake", fake_task)
    db.create_task("t4", "任务4", "fake")
    db.request_cancel("t4")
    task_manager.run_task("fake", {"task_id": "t4", "db_path": None})
    assert called == [], "已取消的任务不应执行"
    assert db.get_task("t4")["status"] == "cancelled"
