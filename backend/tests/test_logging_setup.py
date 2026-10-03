# -*- coding: utf-8 -*-
"""日志体系：结构化 JSON 落盘、任务归档、保留期清理。"""
import json
import logging
import os
import sys
import time
from pathlib import Path

from app import logging_setup


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _record(name: str, level: int, msg: str, args: tuple = (), exc_info=None):
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def test_json_formatter_structured_fields():
    fmt = logging_setup._JsonFormatter()
    record = _record("app.demo", logging.WARNING, "回测降级: %s", ("样本不足",))
    record.task_id = "t-1"
    data = json.loads(fmt.format(record))
    assert data["level"] == "WARNING"
    assert data["logger"] == "app.demo"
    assert data["task_id"] == "t-1"
    assert data["msg"] == "回测降级: 样本不足"


def test_json_formatter_keeps_traceback_and_drops_unserializable():
    fmt = logging_setup._JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("app.demo", logging.ERROR, "失败", exc_info=sys.exc_info())
    record.task_id = ""
    record.obj = object()          # 不可序列化 -> 丢弃，不炸
    data = json.loads(fmt.format(record))
    assert "ValueError: boom" in data["exc"]


def test_task_context_archives_by_task_id():
    task_id = "unit-log-ctx"
    with logging_setup.task_context(task_id, "unit"):
        logging.getLogger("app.unit_test").warning("任务内降级告警")
        logging.getLogger("httpx").warning("第三方噪音")   # 不应进任务归档
    path = Path(logging_setup.config.LOGS_DIR) / "task" / f"{task_id}.log"
    try:
        recs = _records(path)
        assert any(r["msg"] == "任务内降级告警" and r["task_id"] == task_id
                   and r["logger"] == "app.unit_test" for r in recs)
        assert all(r["logger"] != "httpx" for r in recs)
        assert logging_setup.get_task_id() == ""           # 退出后清理上下文
    finally:
        path.unlink(missing_ok=True)


def test_prune_removes_only_expired(tmp_path):
    old = tmp_path / "app-20200101.log"
    old.write_text("{}\n", encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    fresh = task_dir / "t.log"
    fresh.write_text("{}\n", encoding="utf-8")
    logging_setup._prune(tmp_path)
    assert not old.exists()
    assert fresh.exists()
