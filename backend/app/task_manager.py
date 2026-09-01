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


def optimize_task(task_id: str, backtest_config: dict, groups: list, objective: dict,
                  rounds: int, db_path: str, data_dir: str, optuna_dir: str,
                  reports_dir: str) -> None:
    from .optimizer import run_optimize
    summary = run_optimize(
        task_id, backtest_config, groups=groups, objective=objective, rounds=rounds,
        db_path=db_path, data_dir=data_dir, optuna_dir=optuna_dir,
        progress_cb=lambda p, m: db.update_progress(task_id, p, m, db_path))
    path = Path(reports_dir) / f"{task_id}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    db.save_report(task_id, str(path), db_path)
    db.finish_task(task_id, "success",
                   payload={"report_path": str(path),
                            "best_value": summary.get("best_value"),
                            "best_params": summary.get("best_params"),
                            "n_trials": summary.get("n_trials")},
                   db_path=db_path)


def ai_analyze_task(task_id: str, backtest_id: str, profile: str, db_path: str,
                    reports_dir: str, param_importance: Optional[dict] = None,
                    username: Optional[str] = None) -> None:
    from .llm.analyzer import analyze_backtest
    db.update_progress(task_id, 5, "读取回测报告...", db_path)
    report_path = Path(reports_dir) / f"{backtest_id}.json"
    if not report_path.exists():
        raise RuntimeError(f"回测报告不存在: {backtest_id}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    db.update_progress(task_id, 20, "正在调用 LLM 生成分析（深度思考可能需数十秒）...", db_path)
    result = analyze_backtest(report, profile, db_path=db_path,
                               param_importance=param_importance, username=username)
    db.update_progress(task_id, 90, "解析结构化建议...", db_path)
    db.save_analysis(task_id, backtest_id, result["profile"], result["model"], "success",
                     result["content"], result["tokens"], result["elapsed"], None,
                     suggestions=result.get("suggestions"), db_path=db_path)
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


def data_update_task(task_id: str, scope: str, db_path: str, data_dir: str,
                     codes: Optional[list[str]] = None,
                     start_date: str = "1990-01-01",
                     end_date: str = "2099-12-31") -> None:
    from .data.updater import update_task
    update_task(task_id, scope, codes=codes, db_path=db_path, data_dir=data_dir,
                start_date=start_date, end_date=end_date)


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
        self._optimize_executor: Optional[ProcessPoolExecutor] = None
        self._lock = threading.Lock()

    def executor(self) -> ProcessPoolExecutor:
        # 惰性创建（Windows spawn：避免模块导入期副作用）
        with self._lock:
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=3)
            return self._executor

    def optimize_executor(self) -> ProcessPoolExecutor:
        """寻优专用池：max_tasks_per_child=1，每任务独占全新进程、跑完即退出。

        寻优单个 trial 需对大池分钟线做全量 bar dict 物化（数百只 × 数万根，
        峰值数 GB）。常驻 worker 反复 trial 会因堆碎片/分配器保留导致 RSS
        只涨不跌，数十个 trial 后被系统杀掉（表现为「进程池异常终止」）。
        一次性进程由 OS 在任务结束时彻底回收全部内存，根治累积。"""
        with self._lock:
            if self._optimize_executor is None:
                self._optimize_executor = ProcessPoolExecutor(
                    max_workers=1, max_tasks_per_child=1)
            return self._optimize_executor

    def submit(self, kind: str, task_id: str, **kwargs) -> None:
        payload = {"task_id": task_id, "db_path": self.db_path,
                   "data_dir": self.data_dir, "reports_dir": self.reports_dir,
                   "optuna_dir": self.optuna_dir, **kwargs}
        # 寻优任务独占一次性进程；其余任务沿用常驻 3-worker 池
        ex = self.optimize_executor() if kind == "optimize" else self.executor()
        fut = ex.submit(run_task, kind, payload)
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
            if self._optimize_executor is not None:
                self._optimize_executor.shutdown(wait=False, cancel_futures=True)
                self._optimize_executor = None


manager = TaskManager()
