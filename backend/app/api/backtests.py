# -*- coding: utf-8 -*-
"""回测任务接口"""
import json
import uuid
from datetime import datetime
from pathlib import Path

import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..engine.strategies import REGISTRY, apply_param_defaults, validate_params
from ..task_manager import manager

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class RiskConfigModel(BaseModel):
    max_position_pct_per_stock: float = 30
    max_total_position_pct: float = 100
    stop_loss_mode: str = "fixed"
    stop_loss_pct: float = 8.0
    atr_period: int = 14
    atr_multiplier: float = 2.0
    take_profit_pct: float = 0
    trailing_stop_pct: float = 0
    max_drawdown_breaker: float = 30
    max_intraday_trades: int = 4


class BacktestRequest(BaseModel):
    name: str = "回测任务"
    strategy_id: str
    params: dict = Field(default_factory=dict)
    risk_config: RiskConfigModel = Field(default_factory=RiskConfigModel)
    universe: list[str]
    start_date: str
    end_date: str
    period: str = "daily"
    initial_capital: float = 1_000_000
    slippage_pct: float = 0.001
    commission_rate: float = 0.0003
    commission_min: float = 5
    stamp_tax: float = 0.001
    transfer_fee: float = 0.00001
    exclude_st: bool = True


def validate_backtest_config(cfg: dict) -> dict:
    """校验并返回填充默认参数后的完整配置（供回测/寻优共用）"""
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    if strategy is None:
        raise HTTPException(status_code=400, detail=f"策略不存在: {cfg.get('strategy_id')}")
    universe = cfg.get("universe") or []
    if not universe:
        raise HTTPException(status_code=400, detail="universe 不能为空")
    period = cfg.get("period", "daily")
    if period not in strategy.periods:
        raise HTTPException(
            status_code=400,
            detail=f"周期 {period} 不在策略 {strategy.id} 支持范围 {strategy.periods} 内")
    start, end = cfg.get("start_date", ""), cfg.get("end_date", "")
    try:
        d1 = datetime.strptime(start, "%Y-%m-%d")
        d2 = datetime.strptime(end, "%Y-%m-%d")
        if d1 >= d2:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="日期不合法（需 YYYY-MM-DD 且 start<end）")
    ok, err = validate_params(cfg.get("strategy_id"), cfg.get("params") or {})
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    # 参数缺失用 schema default 填充后回显
    cfg = dict(cfg)
    cfg["params"] = apply_param_defaults(cfg["strategy_id"], cfg.get("params") or {})
    cfg["risk_config"] = dict(cfg.get("risk_config") or RiskConfigModel().model_dump())
    return cfg


@router.post("")
def create_backtest(req: BacktestRequest, _user: str = Depends(get_current_user)):
    cfg = validate_backtest_config(req.model_dump())
    task_id = "bt_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, cfg.get("name") or "回测任务", "backtest",
                   payload={"strategy_id": cfg["strategy_id"], "period": cfg["period"],
                            "config": cfg})
    manager.submit("backtest", task_id, backtest_config=cfg)
    return {"task_id": task_id, "status": "pending"}


@router.get("")
def list_backtests(_user: str = Depends(get_current_user)):
    out = []
    for t in db.list_tasks("backtest"):
        payload = t.get("payload") or {}
        out.append({
            "task_id": t["task_id"], "name": t["name"], "status": t["status"],
            "created_at": t["created_at"],
            "strategy_id": payload.get("strategy_id", ""),
            "period": payload.get("period", ""),
            "error": t.get("error"),
        })
    return out


@router.get("/{task_id}/status")
def backtest_status(task_id: str, _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task_id": task_id, "status": task["status"],
            "progress": round(task["progress"] or 0, 1),
            "message": task.get("message") or "",
            "error": task.get("error")}


def _load_report(task: dict) -> dict:
    path = task.get("payload", {}).get("report_path") or db.get_report_path(task["task_id"])
    if not path or not Path(path).exists():
        raise HTTPException(status_code=400,
                            detail=f"回测未完成或失败: {task['status']}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


@router.get("/{task_id}/report")
def backtest_report(task_id: str, _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "success":
        detail = f"回测未完成或失败: {task['status']}"
        if task.get("error"):
            detail += f" ({str(task['error']).splitlines()[0]})"
        raise HTTPException(status_code=400, detail=detail)
    report = _load_report(task)
    report["task_id"] = task_id
    return report


@router.get("/{task_id}/kline")
def backtest_kline(task_id: str, code: str = Query(...), 
                   _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "success":
        raise HTTPException(status_code=400, detail=f"回测未完成或失败: {task['status']}")
    report = _load_report(task)
    cfg = report.get("config") or {}
    period = cfg.get("period", "daily")
    from ..engine import datafeed
    loader = datafeed.load_minute5 if period == "minute5" else datafeed.load_daily
    data = loader([code], cfg.get("start_date"), cfg.get("end_date"))
    df = data.get(code)
    bars = []
    name = code
    if df is not None:
        bars = [
            {"date": r["date"],
             "open": round(r["open"] / r["adj_factor"], 4),
             "high": round(r["high"] / r["adj_factor"], 4),
             "low": round(r["low"] / r["adj_factor"], 4),
             "close": round(r["raw_close"], 4),
             "volume": int(r["volume"])}
            for r in df.to_dicts()
        ]
    from ..data import store
    basic = store.read_stock_basic()
    if basic is not None:
        hit = basic.filter(pl.col("code") == code)
        if hit.height:
            name = hit["name"][0]
    marks = [{"time": t["time"], "price": t["price"], "side": t["side"],
              "type": t["type"], "trade_id": t["trade_id"]}
             for t in report.get("trade_log", []) if t["code"] == code]
    return {"code": code, "name": name, "bars": bars, "marks": marks}
