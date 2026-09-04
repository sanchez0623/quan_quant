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
                  reports_dir: str, walk_forward_folds: int = 3) -> None:
    from .optimizer import run_optimize
    summary = run_optimize(
        task_id, backtest_config, groups=groups, objective=objective, rounds=rounds,
        db_path=db_path, data_dir=data_dir, optuna_dir=optuna_dir,
        walk_forward_folds=walk_forward_folds,
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
                    username: Optional[str] = None, data_dir: Optional[str] = None) -> None:
    from .llm.analyzer import analyze_backtest
    db.update_progress(task_id, 5, "读取回测报告...", db_path)
    report_path = Path(reports_dir) / f"{backtest_id}.json"
    if not report_path.exists():
        raise RuntimeError(f"回测报告不存在: {backtest_id}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    db.update_progress(task_id, 20, "正在调用 LLM 生成分析（深度思考可能需数十秒）...", db_path)
    result = analyze_backtest(report, profile, db_path=db_path,
                               param_importance=param_importance, username=username,
                               data_dir=data_dir)
    db.update_progress(task_id, 90, "解析结构化建议...", db_path)
    # ---- 建议自动验证闭环（方案 B4）：同区间重跑建议配置并 A/B 对比 ----
    validation: Optional[dict] = None
    if result.get("suggestions"):
        db.update_progress(task_id, 93, "运行建议验证回测（同区间，可能需数分钟）...", db_path)
        try:
            from .llm import validation as vs
            validation = vs.run_validation_backtest(
                report.get("config") or {}, result["suggestions"],
                report.get("metrics") or {}, data_dir=data_dir)
            db.update_progress(task_id, 97, "AI 复核验证结果...", db_path)
            validation["commentary"] = vs.review_commentary(
                report, validation, profile, db_path=db_path, username=username)
        except Exception as e:  # noqa: BLE001  验证失败不影响分析结论（AI 不为回测失败背锅）
            validation = {"error": f"{e}", "verdict": None}
    db.save_analysis(task_id, backtest_id, result["profile"], result["model"], "success",
                     result["content"], result["tokens"], result["elapsed"], None,
                     suggestions=result.get("suggestions"),
                     diagnostics=result.get("diagnostics"), validation=validation,
                     db_path=db_path)
    db.finish_task(task_id, "success",
                   payload={"backtest_id": backtest_id, "profile": result["profile"],
                            "verdict": (validation or {}).get("verdict")},
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


def live_premarket_task(task_id: str, db_path: str, data_dir: str,
                        update_data: bool = True, push: bool = True) -> None:
    """实盘盘前编排（LIVE_SIGNAL_SYSTEM §5 盘前）：日线增量更新（含完整性守卫）
    → 盘前信号流程（特征重算/重选/gate/退出检查/推送）→ AI 盘前简报（可选）。"""
    from .live import premarket
    if update_data:
        db.update_task(task_id, db_path=db_path, status="running",
                       message="日线增量更新...")
        from .data import updater
        updater.update(scope="daily", data_dir=data_dir,
                       progress_cb=lambda p, m: db.update_progress(
                           task_id, 5 + p * 0.8, m, db_path=db_path))
        from .engine import datafeed
        datafeed.clear_cache()
    db.update_progress(task_id, 88, "盘前信号流程（特征重算/重选/gate/推送）...",
                       db_path=db_path)
    result = premarket.run_premarket(push=push)
    ai_briefing = None
    cfg = _live_cfg()
    if push and cfg.get("ai_briefing", True):
        db.update_progress(task_id, 95, "AI 生成盘前简报...", db_path)
        from .llm import commentary
        from .live import feishu
        ai_briefing = commentary.premarket_briefing(result, db_path=db_path)
        if ai_briefing:
            feishu.send_text(f"【AI盘前点评 {result.get('as_of') or ''}】\n{ai_briefing}")
    db.finish_task(task_id, "success",
                   payload={"as_of": result.get("as_of"),
                            "rebalanced": result.get("rebalanced"),
                            "pool_size": len(result.get("pool") or []),
                            "stale": result.get("stale"),
                            "signals": len(result.get("signals") or []),
                            "pushed": result.get("pushed"),
                            "ai_briefing": ai_briefing},
                   db_path=db_path)


def live_postclose_task(task_id: str, db_path: str, data_dir: str,
                        push: bool = True) -> None:
    """实盘盘后编排（LIVE_SIGNAL_SYSTEM §5 盘后）：当日分钟线合并落库 + 对账卡推送
    → AI 信号质量点评（可选）。"""
    from .live import postclose
    db.update_progress(task_id, 10, "盘后流程（分钟线落库/对账卡）...",
                       db_path=db_path)
    result = postclose.run_postclose(push=push)
    ai_commentary = None
    cfg = _live_cfg()
    if push and cfg.get("ai_commentary", True):
        db.update_progress(task_id, 90, "AI 生成盘后点评...", db_path)
        from .llm import commentary
        from .live import feishu, reports
        today = (result.get("date") or "")
        signals_today = [s for s in db.list_live_signals(limit=300)
                         if (s.get("ts") or "")[:10] == today
                         and s.get("kind") in ("premarket", "intraday")]
        try:
            shadow = reports.shadow_stats()
        except Exception:  # noqa: BLE001
            shadow = None
        try:
            slippage = reports.slippage_stats().get("summary")
        except Exception:  # noqa: BLE001
            slippage = None
        ai_commentary = commentary.postclose_commentary(
            result, signals_today, shadow=shadow, slippage=slippage, db_path=db_path)
        if ai_commentary:
            feishu.send_text(f"【AI盘后点评 {today}】\n{ai_commentary}")
    db.finish_task(task_id, "success",
                   payload={"saved": len(result.get("saved") or []),
                            "skipped": len(result.get("skipped") or []),
                            "positions": result.get("positions"),
                            "equity": result.get("equity"),
                            "pushed": result.get("pushed"),
                            "ai_commentary": ai_commentary},
                   db_path=db_path)


def _live_cfg() -> dict:
    """实盘流程配置（盘前默认 + sig_config 覆盖），供 AI 简报开关读取。"""
    from .live.intraday import _live_cfg
    return _live_cfg()


_TASK_FUNCS = {
    "backtest": backtest_task,
    "optimize": optimize_task,
    "ai": ai_analyze_task,
    "data_demo": data_demo_task,
    "data_update": data_update_task,
    "live_premarket": live_premarket_task,
    "live_postclose": live_postclose_task,
}


def run_task(kind: str, kwargs: dict) -> None:
    """进程池统一入口：捕获所有异常写 tasks.error。

    协作式取消：任务函数内抛 db.TaskCancelled（update_progress 检查点感知
    cancelling 标记）-> 落 cancelled 终态；排队期间已被请求取消的任务
    在入口直接跳过执行。"""
    import inspect
    task_id = kwargs["task_id"]
    db_path = kwargs.get("db_path")
    try:
        t = db.get_task(task_id, db_path=db_path)
        if t and t["status"] == "cancelling":
            db.finish_task(task_id, "cancelled",
                           error="已被用户取消（执行前）", db_path=db_path)
            return
        fn = _TASK_FUNCS[kind]
        sig = inspect.signature(fn)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        fn(**filtered)
    except db.TaskCancelled:
        db.finish_task(task_id, "cancelled", error="已被用户取消", db_path=db_path)
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
