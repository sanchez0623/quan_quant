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
MAX_TOTAL_TRIALS = 2000


class OptimizeGroup(BaseModel):
    name: str = "参数组"
    n_trials: int = Field(default=20, ge=1, le=1000)
    params: dict = Field(default_factory=dict)


class OptimizeObjective(BaseModel):
    metric: str = "annual_return"
    n_windows: int = Field(default=3, ge=1, le=20)
    variance_penalty: float = Field(default=0.5, ge=0, le=5)
    dd_floor: float | None = Field(default=None, le=0)


class OptimizeRequest(BaseModel):
    name: str = "寻优任务"
    backtest_config: dict
    param_space: dict = Field(default_factory=dict)
    n_trials: int = Field(default=50, ge=1, le=1000)
    metric: str = "annual_return"
    # ---- 方案A 新字段（可选；提供 groups 时走分组坐标轮换，否则退化为单组单窗）----
    groups: list[OptimizeGroup] = Field(default_factory=list)
    objective: OptimizeObjective | None = None
    rounds: int = Field(default=1, ge=1, le=10)


def _validate_space(space: dict) -> None:
    for key, sp in space.items():
        if not isinstance(sp, dict) or "type" not in sp:
            raise HTTPException(status_code=400,
                                detail=f"参数 {key} 的搜索空间定义不合法")


@router.post("")
def create_optimize(req: OptimizeRequest, _user: str = Depends(get_current_user)):
    cfg = validate_backtest_config(req.backtest_config)
    if req.groups:
        # ---- 新格式：分组坐标轮换 ----
        groups = []
        total_per_round = 0
        for g in req.groups:
            if not g.params:
                raise HTTPException(status_code=400, detail=f"组「{g.name}」的 params 不能为空")
            _validate_space(g.params)
            total_per_round += g.n_trials
            groups.append({"name": g.name, "n_trials": g.n_trials, "params": g.params})
        obj = req.objective or OptimizeObjective()
        if obj.metric not in VALID_METRICS:
            raise HTTPException(status_code=400,
                                detail=f"metric 需为 {list(VALID_METRICS)} 之一")
        n_trials_total = req.rounds * total_per_round
        if n_trials_total > MAX_TOTAL_TRIALS:
            raise HTTPException(
                status_code=400,
                detail=f"总试验预算 {n_trials_total} 超过上限 {MAX_TOTAL_TRIALS}"
                       f"（Σ每组trials × {req.rounds} 轮），请减小 trials 或轮次")
        metric = obj.metric
        rounds = req.rounds
        objective = {"metric": obj.metric, "n_windows": obj.n_windows,
                     "variance_penalty": obj.variance_penalty, "dd_floor": obj.dd_floor}
    else:
        # ---- 旧格式：平铺 -> 包装为单组单窗，行为不变 ----
        if not req.param_space:
            raise HTTPException(status_code=400, detail="param_space 不能为空")
        _validate_space(req.param_space)
        if req.metric not in VALID_METRICS:
            raise HTTPException(status_code=400,
                                detail=f"metric 需为 {list(VALID_METRICS)} 之一")
        groups = [{"name": "全部参数", "n_trials": req.n_trials, "params": req.param_space}]
        rounds = 1
        metric = req.metric
        n_trials_total = req.n_trials
        objective = {"metric": req.metric, "n_windows": 1,
                     "variance_penalty": 0.0, "dd_floor": None}

    task_id = "opt_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, req.name or "寻优任务", "optimize",
                   payload={"metric": metric, "n_trials": n_trials_total,
                            "strategy_id": cfg["strategy_id"],
                            "backtest_config": cfg,
                            "groups": groups, "objective": objective, "rounds": rounds})
    manager.submit("optimize", task_id, backtest_config=cfg,
                   groups=groups, objective=objective, rounds=rounds)
    return {"task_id": task_id, "status": "pending"}


@router.post("/{task_id}/resume")
def resume_optimize(task_id: str, _user: str = Depends(get_current_user)):
    """断点续传：用同一 task_id 重新提交，Optuna load_if_exists 载入既有 trial 续跑。
    适用于进程中断/电脑死机后任务停在 running/pending/failed 的情况。"""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["type"] != "optimize":
        raise HTTPException(status_code=400, detail="仅寻优任务支持断点续传")
    p = task.get("payload") or {}
    groups = p.get("groups") or []
    objective = p.get("objective") or {}
    rounds = int(p.get("rounds") or 1)
    bc = p.get("backtest_config") or {}
    if not groups or not bc:
        raise HTTPException(status_code=400,
                            detail="该任务缺少续传所需配置（groups/backtest_config），无法续跑")
    db.reset_task(task_id)
    manager.submit("optimize", task_id, backtest_config=bc,
                   groups=groups, objective=objective, rounds=rounds)
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
                      "best_params", "best_value", "split_date", "param_space", "config",
                      "groups_schedule", "objective", "rounds_history", "per_group_best",
                      "robustness"):
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
