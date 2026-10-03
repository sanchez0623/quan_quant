# -*- coding: utf-8 -*-
"""统一日志体系：控制台可读 + ``data/logs/`` 结构化 JSON 落盘。

- 进程级：``setup()`` 配置 root logger——控制台（人读）+ ``data/logs/app-YYYYMMDD.log``
  （JSON Lines，机器读）。按自然日分文件，不做改名轮转，规避多进程竞争。
- 任务级：``task_context(task_id)`` 在任务执行期间把 ``app.*`` 的全部日志额外写入
  ``data/logs/task/<task_id>.log``（含 task_id / 阶段 / 异常堆栈），任务结束即关闭句柄。
  进程池 worker 由 ``task_manager.run_task`` 自动进出该上下文，业务代码只需
  ``logging.getLogger(__name__)``，无需关心 task_id。
- 保留策略：``setup()`` 时清理超过 ``LOG_RETENTION_DAYS``（默认 30 天）的日志文件。

约定：业务模块统一用 ``logging.getLogger(__name__)``；「静默 except」改为记录 + 降级，
可预期的高频失败（多源探测、可选增强项）用 ``logger.debug(..., exc_info=True)``，
影响结果的降级用 ``logger.warning``。

级别语义：root logger 恒为 DEBUG（记录不丢），由各 handler 过滤——
控制台与 ``app-*.log`` 取 ``LOG_LEVEL``（默认 INFO），任务归档文件恒为 DEBUG，
因此单个任务的全量细节（含被降级的异常）始终可从 ``data/logs/task/<task_id>.log`` 追溯。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config

# 当前任务 id（线程局部；进程池 worker 一次只跑一个任务，主进程调度线程各自独立）
_TASK_ID = threading.local()
_LOCK = threading.Lock()
_configured = False

# LogRecord 内置字段——JSON 落盘时不作为 extra 透传
_STD_ATTRS = frozenset(
    "name msg args levelname levelno pathname filename module exc_info exc_text "
    "stack_info lineno funcName created msecs relativeCreated thread threadName "
    "processName process taskName task_id".split()
)


def get_task_id() -> str:
    """当前线程正在执行的任务 id（无则空串）。"""
    return getattr(_TASK_ID, "value", "") or ""


class _JsonFormatter(logging.Formatter):
    """JSON Lines 格式：固定字段 + 调用方 ``extra`` 透传（不可序列化字段丢弃）。"""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "task_id": getattr(record, "task_id", "") or "",
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    """控制台格式：时间 + 级别 + logger 名 + 任务短 id + 消息。"""

    def format(self, record: logging.LogRecord) -> str:
        task = getattr(record, "task_id", "") or ""
        prefix = f"[{task[:8]}] " if task else ""
        head = (f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
                f"{record.name}: {prefix}")
        out = head + record.getMessage()
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info).rstrip()
        return out


class _TaskIdFilter(logging.Filter):
    """给每条日志打上当前线程的任务 id（root handler 级，业务代码零感知）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = get_task_id()
        return True


class _DailyJsonHandler(logging.FileHandler):
    """按自然日切分的 JSON 文件处理器。

    文件名内嵌日期、不重命名，避免多进程 + RotatingFileHandler 的轮转竞争
    （Windows 上重命名被占用的文件会失败导致日志丢失）。
    """

    def __init__(self, log_dir: Path, prefix: str = "app", encoding: str = "utf-8"):
        self._log_dir = Path(log_dir)
        self._prefix = prefix
        self._day = ""
        super().__init__(self._path("startup"), mode="a", encoding=encoding, delay=True)

    def _path(self, day: str) -> str:
        return str(self._log_dir / f"{self._prefix}-{day}.log")

    def _switch(self, day: str) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.flush()
                stream.close()
        self._day = day
        self.baseFilename = os.path.abspath(self._path(day))

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.fromtimestamp(record.created).strftime("%Y%m%d")
        if day != self._day:
            self._switch(day)
        super().emit(record)


def _prune(log_dir: Path) -> None:
    """清理超过保留期的日志文件（LOG_RETENTION_DAYS，<=0 关闭清理）。"""
    try:
        days = int(os.environ.get("LOG_RETENTION_DAYS", "30") or 30)
    except ValueError:
        days = 30
    if days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=days)
    for sub in (log_dir, log_dir / "task"):
        if not sub.is_dir():
            continue
        for f in sub.glob("*.log"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
            except OSError:
                continue


def setup(level: Optional[str] = None) -> None:
    """初始化进程级日志（幂等，可在子进程内重复调用）。"""
    global _configured
    with _LOCK:
        if _configured:
            return
        log_dir = Path(config.LOGS_DIR)
        (log_dir / "task").mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)   # 根上不过滤，由各 handler 分级
        task_filter = _TaskIdFilter()
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_ConsoleFormatter())
        console.addFilter(task_filter)
        root.addHandler(console)
        daily = _DailyJsonHandler(log_dir, "app")
        daily.setFormatter(_JsonFormatter())
        daily.addFilter(task_filter)
        root.addHandler(daily)
        # 渠道过滤：console/归档按 LOG_LEVEL（默认 INFO）；任务文件由 _open_task_handler
        # 单独设为 DEBUG，保证单任务全量细节可追溯
        channel_level = getattr(logging, (level or os.environ.get("LOG_LEVEL") or "INFO").upper(),
                                logging.INFO)
        console.setLevel(channel_level)
        daily.setLevel(channel_level)
        _configured = True
        _prune(log_dir)
        logging.getLogger(__name__).info("日志系统就绪: %s", log_dir)


def _open_task_handler(task_id: str) -> logging.Handler:
    path = Path(config.LOGS_DIR) / "task" / f"{task_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(logging.Filter("app"))  # 只收应用自身日志，挡掉第三方库噪音
    handler.setLevel(logging.DEBUG)           # 任务归档不吃 LOG_LEVEL，保留全量细节
    root = logging.getLogger()
    with _LOCK:
        root.addHandler(handler)
    return handler


@contextmanager
def task_context(task_id: str, kind: str = "") -> Iterator[str]:
    """任务链路上下文：把该任务的日志额外归档到 ``data/logs/task/<task_id>.log``。"""
    setup()
    handler = _open_task_handler(task_id)
    prev = getattr(_TASK_ID, "value", None)
    _TASK_ID.value = task_id
    log = logging.getLogger("app.task")
    try:
        log.info("任务开始: %s", kind or task_id, extra={"kind": kind or ""})
        yield task_id
    finally:
        with contextlib.suppress(Exception):
            log.info("任务结束", extra={"kind": kind or ""})
        _TASK_ID.value = prev
        with _LOCK:
            root = logging.getLogger()
            with contextlib.suppress(ValueError):
                root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
