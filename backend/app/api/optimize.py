# -*- coding: utf-8 -*-
"""参数寻优接口（Optuna）"""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..task_manager import manager
from .backtests import validate_backtest_config

router = APIRouter(prefix="/api/optimize", tags=["optimize"])

VALID_METRICS = ("annual_return", "sharpe", "calmar", "total_return")


class OptimizeRequest(BaseModel):
    name: str = "寻优任务"
    backtest_config: dict
    param_space: dict = Field(default_factory=dict)
    n_trials: int = Field(default=50, ge=1, le=1000)
    metric: str = "annual_return"


@router.post("")
def create_optimize(req: OptimizeRequest, _user: str = Depends(get_current_user)):
    cfg = validate_backtest_config(req.backtest_config)
    if not req.param_space:
        raise HTTPException(status_code=400, detail="param_space 不能为空")
    for key, sp in req.param_space.items():
        if not isinstance(sp, dict) or "type" not in sp:
            raise HTTPException(status_code=400, detail=f"参数 {key} 的搜索空间定义不合法")
    if req.metric not in VALID_METRICS:
        raise HTTPException(status_code=400,
                            detail=f"metric 需为 {list(VALID_METRICS)} 之一")
    task_id = "opt_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, req.name or "寻优任务", "optimize",
                   payload={"metric": req.metric, "n_trials": req.n_trials,
                            "strategy_id": cfg["strategy_id"],
                            "backtest_config": cfg})
    manager.submit("optimize", task_id, backtest_config=cfg,
                   param_space=req.param_space, n_trials=req.n_trials,
                   metric=req.metric)
    return {"task_id": task_id, "status": "pending"}


@router.get("")
def list_optimizations(_user: str = Depends(get_current_user)):
    out = []
    for t in db.list_tasks("optimize"):
        p = t.get("payload") or {}
        out.append({"task_id": t["task_id"], "name": t["name"], "status": t["status"],
                    "created_at": t["created_at"],
                    "best_value": p.get("best_value"),
                    "best_params": p.get("best_params"),
                    "n_trials": p.get("n_trials")})
    return out


@router.get("/{task_id}")
def optimize_detail(task_id: str, _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload = task.get("payload") or {}
    base = {"task_id": task_id, "status": task["status"],
            "progress": round(task["progress"] or 0, 1),
            "metric": payload.get("metric", "annual_return"),
            "n_trials": payload.get("n_trials", 0),
            "best_params": payload.get("best_params"),
            "best_value": payload.get("best_value"),
            "backtest_config": payload.get("backtest_config"),
            "trials": [], "param_importance": None, "oos_validation": None,
            "error": task.get("error")}
    if task["status"] == "success":
        path = payload.get("report_path") or db.get_report_path(task_id)
        if path and Path(path).exists():
            summary = json.loads(Path(path).read_text(encoding="utf-8"))
            for k in ("trials", "param_importance", "oos_validation",
                      "best_params", "best_value", "split_date", "param_space", "config"):
                if k in summary:
                    base[k] = summary[k]
            if base.get("backtest_config") is None and "config" in summary:
                base["backtest_config"] = summary["config"]  # 旧报告兼容
    elif task["status"] == "running":
        # 运行中：从 Optuna study 读已完成 trials
        try:
            import optuna
            db_file = Path(manager.optuna_dir) / f"{task_id}.db"
            if db_file.exists():
                study = optuna.load_study(
                    study_name=task_id,
                    storage=f"sqlite:///{str(db_file).replace(chr(92), '/')}")
                base["trials"] = [
                    {"number": t.number, "params": t.params,
                     "value": (round(t.value, 6) if t.value is not None else None),
                     "state": t.state.name.lower()} for t in study.trials]
                done = [t for t in study.trials if t.value is not None]
                if done:
                    best = max(done, key=lambda t: t.value)
                    base["best_params"], base["best_value"] = best.params, round(best.value, 6)
        except Exception:  # noqa: BLE001
            pass
    return base
