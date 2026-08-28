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
    stop_loss_pct: float = 12.0
    atr_period: int = 14
    atr_multiplier: float = 2.5
    take_profit_pct: float = 0
    trailing_stop_pct: float = 5.0
    max_drawdown_breaker: float = 30
    max_intraday_trades: int | None = None  # 未传时自动对齐策略 max_t_times
    max_holdings: int = 0              # 最大持仓只数，0=不限
    cash_reserve_pct: float = 1.5      # 现金缓冲比例（永不进场的资金）


class BacktestRequest(BaseModel):
    name: str = "回测任务"
    strategy_id: str
    params: dict = Field(default_factory=dict)
    risk_config: RiskConfigModel = Field(default_factory=RiskConfigModel)
    universe: list[str]
    # 条件选股溯源（UNIVERSE_PICKER §7）：池子的来历与 seed，模板载入/实验复现可审计
    universe_meta: dict | None = None
    start_date: str
    end_date: str
    period: str = "daily"
    initial_capital: float = 1_000_000
    slippage_pct: float = 0.001
    # ---- 交易成本（2026年现行费率默认值）----
    commission_rate: float = 0.00005   # 佣金 万0.5（双边）
    commission_min: float = 5          # 最低佣金（元）
    stamp_tax: float = 0.0005          # 印花税 万5（仅卖出）
    transfer_fee: float = 0.00001      # 过户费 万0.1（双边）
    handling_fee: float = 0.0000341    # 经手费 万0.341（双边）
    regulatory_fee: float = 0.00002    # 证管费 万0.2（双边）
    exclude_st: bool = True
    # ---- 指标预热（0=使用策略建议的预热期）----
    warmup_days: int = 0
    # ---- 月度出金（0=关闭）----
    monthly_withdraw_base: float = 0       # 每月提取目标额，不足月末补齐
    t_profit_withdraw_pct: float = 10      # 每笔做T盈利即时提取比例（%）
    min_t_amount: float = 20000            # 做T卖出最小金额（防碎单费用磨损）


def _norm_universe(universe: list[str]) -> list[str]:
    """归一化股票代码为纯数字（sh.600021 / sh600021 -> 600021），去重保序"""
    from ..data.sources import _norm_code
    out, seen = [], set()
    for c in universe or []:
        c = _norm_code(str(c).strip())
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def validate_backtest_config(cfg: dict) -> dict:
    """校验并返回填充默认参数后的完整配置（供回测/寻优共用）"""
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    if strategy is None:
        raise HTTPException(status_code=400, detail=f"策略不存在: {cfg.get('strategy_id')}")
    universe = _norm_universe(cfg.get("universe") or [])
    if not universe:
        raise HTTPException(status_code=400, detail="universe 不能为空")
    cfg = dict(cfg)
    cfg["universe"] = universe
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
    risk = dict(cfg.get("risk_config") or RiskConfigModel().model_dump())
    # 日内交易次数默认对齐策略 max_t_times（未显式配置时）；max_t_times=0（关闭做T）时
    # 移除该键，让 RiskConfig 落到默认值 4，避免 None 进入 int() 或误拦趋势交易
    if not risk.get("max_intraday_trades"):
        mt = int(cfg["params"].get("max_t_times") or 0)
        if mt > 0:
            risk["max_intraday_trades"] = mt
        else:
            risk.pop("max_intraday_trades", None)
    cfg["risk_config"] = risk
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
            "config": payload.get("config"),
            "error": t.get("error"),
        })
    return out


# ---------------- 回测配置模板（每用户私有） ----------------

class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    config: dict


@router.get("/templates")
def list_templates(user: str = Depends(get_current_user)):
    return db.list_templates(user)


@router.post("/templates")
def add_template(req: TemplateCreate, user: str = Depends(get_current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名不能为空")
    if not req.config.get("strategy_id"):
        raise HTTPException(status_code=400, detail="配置缺少 strategy_id，无法保存为模板")
    template_id = db.add_template(user, name, req.config)
    return {"id": template_id, "status": "ok"}


@router.delete("/templates/{template_id}")
def remove_template(template_id: int, user: str = Depends(get_current_user)):
    if not db.delete_template(template_id, user):
        raise HTTPException(status_code=404, detail="模板不存在或不属于当前用户")
    return {"status": "ok"}


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


@router.delete("/{task_id}")
def delete_backtest(task_id: str, _user: str = Depends(get_current_user)):
    """删除回测任务（含报告文件与关联的 AI 分析）；运行中的任务不允许删除"""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["type"] != "backtest":
        raise HTTPException(status_code=400, detail="仅支持删除回测任务")
    if task["status"] in ("pending", "running"):
        raise HTTPException(status_code=400, detail="回测运行中，请等待完成后再删除")
    path = task.get("payload", {}).get("report_path") or db.get_report_path(task_id)
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass  # 文件已不存在或删除失败不阻塞记录删除
    db.delete_task(task_id)
    return {"status": "ok"}


@router.get("/{task_id}/kline")
def backtest_kline(task_id: str, code: str = Query(...),
                   period: str = Query(None),
                   _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "success":
        raise HTTPException(status_code=400, detail=f"回测未完成或失败: {task['status']}")
    report = _load_report(task)
    cfg = report.get("config") or {}
    # 图表周期可覆盖回测周期（如 5 分钟回测切日线更易观察交易点）
    period = period or cfg.get("period", "daily")
    if period not in ("daily", "minute5"):
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
    # 日线视图下把交易时间归一化到"日"，同一日多笔交易映射到同一根K线（前端错开标注）
    def _mark_time(t: dict) -> str:
        return t["time"][:10] if period == "daily" else t["time"]
    marks = [{"time": _mark_time(t), "price": t["price"], "side": t["side"],
              "type": t["type"], "trade_id": t["trade_id"]}
             for t in report.get("trade_log", []) if t["code"] == code]
    return {"code": code, "name": name, "bars": bars, "marks": marks}
