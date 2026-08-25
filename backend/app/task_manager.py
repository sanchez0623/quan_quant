# -*- coding: utf-8 -*-
"""任务系统：ProcessPoolExecutor(max_workers=3) + 子进程直接写 SQLite 进度 + 崩溃兜底。
Windows spawn 兼容：任务函数均为模块级可 pickle；executor 惰性创建。
"""
import json
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from . import config, db


# ---------------- 模块级任务函数（可 pickle，子进程执行） ----------------

def backtest_task(task_id: str, backtest_config: dict, db_path: str, data_dir: str,
                  reports_dir: str) -> None:
    from .engine import datafeed, runner
    datafeed.clear_cache()
    cfg = dict(backtest_config)
    cfg["task_id"] = task_id

    def cb(p: float, m: str) -> None:
        db.update_progress(task_id, p, m, db_path)

    db.update_task(task_id, db_path=db_path, status="running", message="加载数据...")
    report = runner.run_backtest(cfg, data_dir=data_dir, progress_cb=cb)
    path = Path(reports_dir) / f"{task_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    db.save_report(task_id, str(path), db_path)
    db.finish_task(task_id, "success",
                   payload={"report_path": str(path), "metrics": report["metrics"]},
                   db_path=db_path)


def optimize_task(task_id: str, backtest_config: dict, param_space: dict, n_trials: int,
                  metric: str, db_path: str, data_dir: str, optuna_dir: str,
                  reports_dir: str) -> None:
    from .optimizer import run_optimize
    summary = run_optimize(
        task_id, backtest_config, param_space, n_trials, metric,
        db_path=db_path, data_dir=data_dir, optuna_dir=optuna_dir,
        progress_cb=lambda p, m: db.update_progress(task_id, p, m, db_path))
    path = Path(reports_dir) / f"{task_id}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    db.save_report(task_id, str(path), db_path)
    db.finish_task(task_id, "success",
                   payload={"report_path": str(path),
                            "best_value": summary.get("best_value"),
                            "best_params": summary.get("best_params"),
                            "n_trials": n_trials},
                   db_path=db_path)


def ai_analyze_task(task_id: str, backtest_id: str, profile: str, db_path: str,
                    reports_dir: str, param_importance: Optional[dict] = None) -> None:
    from .llm.analyzer import analyze_backtest
    report_path = Path(reports_dir) / f"{backtest_id}.json"
    if not report_path.exists():
        raise RuntimeError(f"回测报告不存在: {backtest_id}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = analyze_backtest(report, profile, db_path=db_path,
                               param_importance=param_importance)
    db.save_analysis(task_id, backtest_id, result["profile"], result["model"], "success",
                     result["content"], result["tokens"], result["elapsed"], None, db_path)
    db.finish_task(task_id, "success",
                   payload={"backtest_id": backtest_id, "profile": result["profile"]},
                   db_path=db_path)


def data_demo_task(task_id: str, stocks: Optional[list], days: int,
                   db_path: str, data_dir: str) -> None:
    from .data import synthetic
    from .engine import datafeed
    db.update_task(task_id, db_path=db_path, status="running", message="生成演示数据...")
    stats = synthetic.generate_demo_data(stocks=stocks, days=days, data_dir=data_dir)
    datafeed.clear_cache()
    db.finish_task(task_id, "success", payload=stats, db_path=db_path)


def data_update_task(task_id: str, scope: str, db_path: str, data_dir: str) -> None:
    from .data.updater import update_task
    update_task(task_id, scope, db_path=db_path, data_dir=data_dir)


_TASK_FUNCS = {
    "backtest": backtest_task,
    "optimize": optimize_task,
    "ai": ai_analyze_task,
    "data_demo": data_demo_task,
    "data_update": data_update_task,
}


def run_task(kind: str, kwargs: dict) -> None:
    """进程池统一入口：捕获所有异常写 tasks.error"""
    import inspect
    task_id = kwargs["task_id"]
    db_path = kwargs.get("db_path")
    try:
        fn = _TASK_FUNCS[kind]
        sig = inspect.signature(fn)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        fn(**filtered)
    except Exception as e:  # noqa: BLE001
        db.finish_task(task_id, "failed",
                       error=f"{e}\n{traceback.format_exc()[-1500:]}", db_path=db_path)


# ---------------- 主进程 TaskManager ----------------

class TaskManager:
    def __init__(self, db_path: Optional[str] = None, data_dir: Optional[str] = None,
                 reports_dir: Optional[str] = None, optuna_dir: Optional[str] = None):
        self.db_path = db_path or str(config.META_DB_PATH)
        self.data_dir = data_dir or str(config.DATA_DIR)
        self.reports_dir = reports_dir or str(config.REPORTS_DIR)
        self.optuna_dir = optuna_dir or str(config.OPTUNA_DIR)
        self._executor: Optional[ProcessPoolExecutor] = None
        self._lock = threading.Lock()

    def executor(self) -> ProcessPoolExecutor:
        # 惰性创建（Windows spawn：避免模块导入期副作用）
        with self._lock:
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=3)
            return self._executor

    def submit(self, kind: str, task_id: str, **kwargs) -> None:
        payload = {"task_id": task_id, "db_path": self.db_path,
                   "data_dir": self.data_dir, "reports_dir": self.reports_dir,
                   "optuna_dir": self.optuna_dir, **kwargs}
        fut = self.executor().submit(run_task, kind, payload)
        fut.add_done_callback(lambda f, tid=task_id: self._on_done(f, tid))

    def _on_done(self, fut, task_id: str) -> None:
        """worker 崩溃兜底：future 异常而任务未达终态 → 标记 failed"""
        exc = fut.exception()
        if exc is None:
            return
        try:
            task = db.get_task(task_id, db_path=self.db_path)
            if task and task["status"] not in ("success", "failed", "cancelled"):
                db.finish_task(task_id, "failed", error=f"任务进程异常终止: {exc}",
                               db_path=self.db_path)
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None


manager = TaskManager()
