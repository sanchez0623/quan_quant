# -*- coding: utf-8 -*-
"""回测任务接口"""
import json
import uuid
from datetime import datetime
from pathlib import Path

import logging
import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, create_model

from .. import db
from ..auth import get_current_user
from ..engine.strategies import REGISTRY, apply_param_defaults, validate_params
from ..task_manager import manager
from .backtest_schema import (
    RANK_KEY_LABELS,
    TOP_FIELDS,
    UI_FIELDS,
    pydantic_field_spec,
    top_level_defaults,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class RiskConfigModel(BaseModel):
    max_position_pct_per_stock: float = 40
    max_total_position_pct: float = 100
    stop_loss_mode: str = "atr_trailing"
    stop_loss_pct: float = 12.0
    atr_period: int = 14
    atr_multiplier: float = 2.5
    take_profit_pct: float = 40
    trailing_stop_pct: float = 5.0
    max_drawdown_breaker: float = 30
    max_intraday_trades: int | None = None  # 未传时自动对齐策略 max_t_times
    max_holdings: int = 0              # 最大持仓只数，0=不限
    cash_reserve_pct: float = 1.5      # 现金缓冲比例（永不进场的资金）
    # ---- 组合层：板块集中度上限（main/chinext/star/bse，0=不启用；只限开仓/加仓）----
    max_sector_pct: float = 0.0        # 单板块持仓市值 ≤ 净值 × 该值/100
    # ---- atr_trailing：止损线 = max(成本项−k1×ATR, 最高价−k2×ATR)，只上不下 ----
    atr_trail_mult: float = 6.0        # k2：移动锁盈倍数（5~12 稳健区间，<=3 偏紧）
    atr_cost_base: str = "first"       # 成本基准：first=首笔开仓价｜wavg=加权平均成本
    atr_trail_floor: bool = True       # 棘轮：止损线只上不下
    # ---- 自适应止损：按市场状态缩放 k1/k2 ----
    adaptive: str = "trend"            # off｜trend=个股趋势｜vol=波动率分位
    adaptive_trend_ma: int = 60
    adaptive_slope_n: int = 5
    adaptive_k_loose: float = 1.5      # 趋势确立 -> 放宽，让利润奔跑
    adaptive_k_tight: float = 0.7      # 趋势破坏 -> 收紧，快速离场
    adaptive_vol_n: int = 120
    adaptive_vol_hi: float = 0.7
    adaptive_vol_lo: float = 0.3
    # ---- 双层止损（方案B）：交易仓(做T)独立档，默认关。默认档取敏感度实测最优 sp=10/tm=5 ----
    trade_tier_on: bool = False
    trade_atr_mult: float = 3.0
    trade_trail_mult: float = 5.0
    trade_stop_pct: float = 10.0
    # ---- 方案E：市况条件化保护（regime_b_on=on 时双层止损只在趋势市启用）----
    regime_b_on: bool = False


def _build_backtest_request_model():
    """由 backtest_schema.TOP_FIELDS 生成 BacktestRequest（字段与默认值单一来源）。

    此前手写模型漏登记了 pool_refill_min，pydantic 直接静默丢弃前端传值、
    后端再兜底 2，导致用户手动建的任务被静默配成已证伪组合（历史事故）。
    改为生成式后，「模型漏字段」在结构上不可能发生。
    """
    fields: dict[str, tuple] = {}
    for f in TOP_FIELDS:
        if f["type"] == "risk_config":
            fields[f["key"]] = (RiskConfigModel,
                                Field(default_factory=RiskConfigModel, description=f["label"]))
            continue
        annotation, field_info = pydantic_field_spec(f)
        fields[f["key"]] = (annotation, field_info)
    return create_model("BacktestRequest", **fields)


BacktestRequest = _build_backtest_request_model()


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


def normalize_config(cfg: dict) -> dict:
    """把配置按「策略 param_schema + RiskConfigModel」补全到全量，并剪掉不属于当前策略的键。

    存在的必要性（两类已在线上出现过的问题）：
    1. 参数改版新增参数后，历史模板/历史配置缺新键，引擎会静默回落到 RiskConfig
       的内置默认值，而不是用户在表单里调过的值（如 atr_multiplier、adaptive_*）；
    2. 前端 setFieldsValue 是深合并，切换策略后旧策略的参数会残留在 params 里
       （实测模板 #15「测试新策略」momentum_slot 带着 ma_cross 的
       fast / slow / stop_loss_pct），而 runner 有「params 含 stop_loss_pct 且
       risk_config 未显式给 -> 覆盖风控止损」的兼容分支，残留值会劫持止损。

    回测 / 寻优 / 对比实验 / 模板读写统一走这里，保证落库配置恒为「全量且干净」。
    """
    cfg = dict(cfg)
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    risk_fields = RiskConfigModel.model_fields
    risk = {k: v for k, v in (cfg.get("risk_config") or {}).items() if k in risk_fields}

    params = dict(cfg.get("params") or {})
    if strategy is not None:
        schema_keys = {p["key"] for p in strategy.param_schema}
        params = apply_param_defaults(cfg["strategy_id"], params)
        # 不属于本策略的键一律剔除；其中「其实是风控字段」的（寻优把风控参数一起塞进
        # best_params 的历史数据，如模板 #9 的 params 里躺着 11 个风控键）归位到
        # risk_config 而不是丢掉。
        # 注意：只在 risk_config 没显式给该键时才补——risk_config 是权威来源，
        # 不能用 params 里的错位值去覆盖用户明确的止损设置（否则会静默改止损）。
        for k in list(params):
            if k in schema_keys:
                continue
            v = params.pop(k)
            if k in risk_fields and v is not None and k not in risk:
                risk[k] = v
    cfg["params"] = params

    for k, v in RiskConfigModel().model_dump().items():
        # max_intraday_trades 默认 None：留空交给调用方对齐策略 max_t_times
        if k not in risk and v is not None:
            risk[k] = v
    cfg["risk_config"] = risk

    # 顶层字段默认值来自 backtest_schema.TOP_FIELDS（单一事实源，fill=True 的条目）。
    # 新增顶层字段只改那里；前端默认值由 GET /api/backtests/meta 下发。
    for k, v in top_level_defaults().items():
        if cfg.get(k) is None:
            cfg[k] = v
    return cfg


def validate_backtest_config(cfg: dict) -> dict:
    """校验并返回填充默认参数后的完整配置（供回测/寻优共用）"""
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    if strategy is None:
        raise HTTPException(status_code=400, detail=f"策略不存在: {cfg.get('strategy_id')}")
    universe = _norm_universe(cfg.get("universe") or [])
    auto_mode = bool(cfg.get("universe_auto"))
    if auto_mode:
        if cfg.get("strategy_id") not in ("momentum_t", "momentum_slot"):
            raise HTTPException(status_code=400,
                                detail="动态选股（universe_auto）仅支持 momentum_t / momentum_slot")
        if universe:
            raise HTTPException(status_code=400,
                                detail="动态选股开启时 universe 应留空（池子由动量预筛自动生成）")
        idle_n = cfg.get("auto_idle_days")
        if idle_n is not None and (not isinstance(idle_n, int) or idle_n < 1 or idle_n > 60):
            raise HTTPException(status_code=400, detail="auto_idle_days 需为 1~60 的整数")
        refill = cfg.get("pool_refill_min")
        if refill is not None and (not isinstance(refill, int) or refill < 0 or refill > 50):
            raise HTTPException(status_code=400, detail="pool_refill_min 需为 0~50 的整数（0=关闭枯竭换血）")
        top_x = cfg.get("auto_top_x")
        if top_x is not None and (not isinstance(top_x, int) or top_x < 1 or top_x > 500):
            raise HTTPException(status_code=400, detail="auto_top_x 需为 1~500 的整数")
        above_ma = cfg.get("auto_above_ma")
        if above_ma is not None and (not isinstance(above_ma, int) or above_ma < 5 or above_ma > 120):
            raise HTTPException(status_code=400, detail="auto_above_ma 需为 5~120 的整数")
        min_rps = cfg.get("auto_min_rps")
        if min_rps is not None and (not isinstance(min_rps, (int, float))
                                    or not 0 <= float(min_rps) <= 100):
            raise HTTPException(status_code=400, detail="auto_min_rps 需为 0~100 的数值")
        from ..engine.momentum_core import RANK_KEYS
        rank_key = cfg.get("auto_rank_key")
        if rank_key is not None and rank_key not in RANK_KEYS:
            raise HTTPException(status_code=400,
                                detail=f"auto_rank_key（排序键）需为 {sorted(RANK_KEYS)} 之一")
        from ..data.sources import INDEX_REGISTRY, INDEX_CSI800, BOARD_LABELS
        bad_idx = [k for k in (cfg.get("auto_index") or [])
                   if k not in {INDEX_CSI800, *INDEX_REGISTRY}]
        if bad_idx:
            raise HTTPException(status_code=400,
                                detail=f"auto_index 含未知指数: {bad_idx}（合法: sz50/hs300/zz500/csi800）")
        bad_boards = [b for b in (cfg.get("auto_boards") or []) if b not in BOARD_LABELS]
        if bad_boards:
            raise HTTPException(status_code=400,
                                detail=f"auto_boards 含未知板块: {bad_boards}（合法: main/chinext/star/bse）")
    # ---- 基准对比校验（BENCHMARK，全策略通用）----
    from ..data.sources import INDEX_DAILY_CODES
    bench = cfg.get("benchmark")
    if bench is not None and bench not in INDEX_DAILY_CODES:
        raise HTTPException(status_code=400,
                            detail=f"benchmark（基准指数）需为 {sorted(INDEX_DAILY_CODES)} 之一")
    # ---- 池级趋势开关校验（POOL_GATE）----
    if cfg.get("pool_gate"):
        if cfg.get("strategy_id") not in ("momentum_t", "momentum_slot"):
            raise HTTPException(status_code=400,
                                detail="池级趋势开关仅支持 momentum_t / momentum_slot")
        th = cfg.get("pool_gate_enter_th")
        if th is not None and (not isinstance(th, (int, float))
                               or not 0 < float(th) < 0.5):
            raise HTTPException(status_code=400,
                                detail="pool_gate_enter_th 需为 0~0.5 之间的小数（默认 0.15）")
    # ---- 大盘趋势闸门校验（INDEX_GATE）----
    if cfg.get("index_gate"):
        if cfg.get("strategy_id") not in ("momentum_t", "momentum_slot"):
            raise HTTPException(status_code=400,
                                detail="大盘趋势闸门仅支持 momentum_t / momentum_slot")
    if not auto_mode and not universe \
            and cfg.get("strategy_id") != "dragon_dip_minute":
        # dragon_dip_minute 引擎自取全市场日线上下文（universe 硬编码），
        # 静态池参数不参与候选挖掘（候选=全市场触板票，任何静态池必漏），
        # 故允许 universe 留空
        raise HTTPException(status_code=400, detail="universe 不能为空")
    cfg = dict(cfg)
    cfg["universe"] = universe
    period = cfg.get("period", "daily")
    if period not in strategy.periods:
        raise HTTPException(
            status_code=400,
            detail=f"周期 {period} 不在策略 {strategy.id} 支持范围 {strategy.periods} 内")
    start, end = cfg.get("start_date", ""), cfg.get("end_date", "")
    if cfg.get("end_date_today"):
        end = datetime.now().strftime("%Y-%m-%d")
        cfg["end_date"] = end
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
    # 参数缺失用 schema default 填充后回显；同时剪掉不属于本策略的残留键
    cfg = normalize_config(cfg)
    risk = cfg["risk_config"]
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
def list_backtests(_user: str = Depends(get_current_user),
                   search: str = "", tag: str = "",
                   page: int = 1, page_size: int = 20,
                   include_config: int = 0):
    # 服务端分页（page/page_size，上限 1000）+ 搜索（name/id LIKE）+ 标签筛选
    # （tag=重点 精确匹配；tag=__none__ 只看无标签）。include_config=1 才返回
    # 完整配置（列表展示用不到，"存为模板"需要——响应体大小差一个量级）。
    page = max(1, int(page))
    page_size = min(1000, max(1, int(page_size)))
    tag_arg = tag if tag else None
    total = db.count_tasks("backtest", search=search, tag=tag_arg)
    rows = db.list_tasks("backtest", search=search, tag=tag_arg,
                         limit=page_size, offset=(page - 1) * page_size)
    items = []
    for t in rows:
        payload = t.get("payload") or {}
        cfg = payload.get("config")
        # 归一化后再回显：老任务配置缺改版后新增的参数时，「存为模板」也能拿到全量配置
        if include_config and isinstance(cfg, dict) and cfg.get("strategy_id"):
            try:
                cfg = normalize_config(cfg)
            except Exception:      # 归一化失败不影响列表展示
                logger.warning("归一化失败不影响列表展示", exc_info=True)
        item = {
            "task_id": t["task_id"], "name": t["name"], "status": t["status"],
            "created_at": t["created_at"], "tag": t.get("tag", ""),
            "strategy_id": payload.get("strategy_id", ""),
            "period": payload.get("period", ""),
            "error": t.get("error"),
        }
        if include_config:
            item["config"] = cfg
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------- 回测配置模板（每用户私有） ----------------

class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    config: dict


@router.get("/meta")
def backtest_form_meta(_user: str = Depends(get_current_user)):
    """回测表单顶层字段元数据（默认值 / 选项 / 数值范围的唯一来源）。

    前端 BacktestList 据此生成 initialValues / buildConfigFromValues /
    applyConfigToForm，新增顶层字段只改后端 backtest_schema.TOP_FIELDS，
    前端不再手抄默认值，也不会再出现「前端/引擎默认值不一致」。
    """
    from ..data.sources import BOARD_LABELS, INDEX_CSI800, INDEX_CSI800_NAME, INDEX_REGISTRY
    from ..engine.momentum_core import RANK_KEYS

    dynamic_choices = {
        "rank_key": [{"value": k, "label": RANK_KEY_LABELS.get(k, k)} for k in RANK_KEYS],
        "auto_index": (
            [{"value": k, "label": name} for k, (_, name) in INDEX_REGISTRY.items()]
            + [{"value": INDEX_CSI800, "label": f"{INDEX_CSI800_NAME}（=沪深300+中证500）"}]
        ),
        "auto_boards": [{"value": k, "label": v} for k, v in BOARD_LABELS.items()],
    }

    fields = []
    for f in UI_FIELDS:
        item = dict(f)
        if f.get("choices_from"):
            item["choices"] = dynamic_choices.get(f["choices_from"], [])
        fields.append(item)
    return {"fields": fields}


@router.get("/templates")
def list_templates(user: str = Depends(get_current_user)):
    # 读取时按当前 schema 归一化：参数改版前的老模板缺的新参数即时补全、
    # 跨策略残留键即时剔除，用户不必重新保存即可载入完整配置
    out = []
    for t in db.list_templates(user):
        cfg = t.get("config")
        if isinstance(cfg, dict) and cfg.get("strategy_id"):
            try:
                cfg = normalize_config(cfg)
            except Exception:      # 归一化失败时原样返回，不阻塞列表
                logger.warning("归一化失败时原样返回，不阻塞列表", exc_info=True)
        out.append(dict(t, config=cfg))
    return out


@router.post("/templates")
def add_template(req: TemplateCreate, user: str = Depends(get_current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名不能为空")
    cfg = req.config or {}
    if not cfg.get("strategy_id"):
        raise HTTPException(status_code=400, detail="配置缺少 strategy_id，无法保存为模板")
    # 落库前归一化：保证模板恒为「参数全量 + 无残留键」，与回测提交口径一致
    try:
        cfg = normalize_config(cfg)
    except Exception:
        logger.warning("add_template 失败，降级继续", exc_info=True)
    template_id = db.add_template(user, name, cfg)
    return {"id": template_id, "status": "ok"}


@router.delete("/templates/{template_id}")
def remove_template(template_id: int, user: str = Depends(get_current_user)):
    if not db.delete_template(template_id, user):
        raise HTTPException(status_code=404, detail="模板不存在或不属于当前用户")
    return {"status": "ok"}


# ---------------- AI 生成回测名称（AI_NAME） ----------------

# 有辨识度的策略/风控参数 key（供 AI 命名时挑选），覆盖各策略 param_schema + RiskConfigModel
_NAME_KEY_PARAMS = {
    # momentum_t / momentum_slot 动量系
    "mom_short", "mom_mid", "mom_long", "mom_gap_n", "exit_need", "rps_top",
    "pool_n", "max_adds", "base_pct", "top_n", "add_breakout_n", "with_accel",
    # 做T机制
    "t_mode", "t_debt_max_days", "t_max_chase_pct", "reentry_discount",
    # 双均线 / 网格
    "fast", "slow", "grid_band", "t_pct", "vol_window",
    # 动态选股
    "auto_top_x", "auto_above_ma", "auto_with_accel", "auto_min_rps", "auto_rank_key",
    # 池级趋势开关（POOL_GATE）
    "pool_gate", "pool_gate_enter_th",
    # 风控关键项（对齐 RiskConfigModel）
    "max_holdings", "max_position_pct_per_stock", "stop_loss_mode", "stop_loss_pct",
    "atr_trail_mult", "atr_cost_base", "atr_trail_floor", "take_profit_pct",
    "adaptive", "trailing_stop_pct",
    # 总资金止盈（NAV_TAKE_PROFIT）
    "nav_take_profit_pct", "nav_take_profit_withdraw_pct",
}


def _norm_num(v):
    """数值统一成 float，避免 6 vs 6.0 被误判为非默认值"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _non_default_params(items: dict, defaults: dict) -> dict:
    """只保留「白名单内 + 非默认值」的参数，降低 AI 输入噪音、聚焦差异点"""
    out = {}
    for k, v in (items or {}).items():
        if k not in _NAME_KEY_PARAMS or v is None:
            continue
        if k in defaults and _norm_num(defaults[k]) == _norm_num(v):
            continue
        out[k] = v
    return out


class GenNameRequest(BaseModel):
    strategy_id: str
    params: dict = Field(default_factory=dict)
    risk_config: dict = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    universe_auto: bool = False
    start_date: str = ""
    end_date: str = ""
    period: str = "daily"
    initial_capital: float = 0
    benchmark: str = "000905"


@router.post("/generate-name")
def generate_name(req: GenNameRequest, user: str = Depends(get_current_user)):
    """用当前策略配置让 AI 生成一个合适的回测任务名（轻量单次调用，不落库）。"""
    from ..llm import provider
    if not provider.db_key_entries(user) and not provider.key_pool_mode():
        raise HTTPException(status_code=400,
                            detail="未配置 LLM API Key：请到「Key 管理」页添加 API Key（DeepSeek/OpenRouter 等）")
    strategy = REGISTRY.get(req.strategy_id)
    sname = getattr(strategy, "name", None) or req.strategy_id
    # 默认值对照：param_schema default / RiskConfigModel 默认，仅挑“非默认值”差异项
    param_defaults = apply_param_defaults(req.strategy_id, {})
    risk_defaults = RiskConfigModel().model_dump()
    key_params = _non_default_params(req.params, param_defaults)
    key_risk = _non_default_params(req.risk_config, risk_defaults)
    # 全默认时退化为全量摘要（截断），避免 AI 无从命名
    if not key_params:
        full = json.dumps(req.params or {}, ensure_ascii=False)
        if len(full) > 400:
            full = full[:400] + "..."
        key_params = full
    summary = {
        "策略": sname,
        "周期": req.period,
        "时间": f"{req.start_date} ~ {req.end_date}",
        "初始资金": round(req.initial_capital, 0),
        "动态选股": bool(req.universe_auto),
        "股票数": "自动生成" if req.universe_auto else len(req.universe or []),
        "基准": req.benchmark,
        "差异参数": key_params,
        "风控差异": key_risk,
    }
    prompt = json.dumps(summary, ensure_ascii=False)
    messages = [
        {"role": "system", "content": "你是 A 股量化回测任务的命名助手。用户会给出策略与参数配置，"
                                      "其中「差异参数/风控差异」是相对默认值被改动过的关键项。"
                                      "请抓住 2~4 个最能体现本次回测特色的差异点（如：动量窗、做T、仓位、"
                                      "风控收紧、总资金止盈等）生成一个简洁、信息量高的中文任务名（6~24 字）。"
                                      "名称应体现策略/周期/资金档特征 + 关键差异，不要堆砌默认值。"
                                      "只输出名称本身，不要任何解释、引号或前后缀。"},
        {"role": "user", "content": f"配置摘要：{prompt}\n请生成一个合适的回测任务名。"},
    ]
    try:
        res = provider.chat(None, messages, temperature=0.5, db_path=None, username=user)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"AI 生成名称失败：{e}")
    name = (res.get("content") or "").strip().strip('"').strip("'").replace("\n", " ").strip()
    if not name:
        raise HTTPException(status_code=400, detail="AI 未返回有效名称，请重试")
    return {"name": name[:40], "model": res.get("model")}


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
             "volume": int(r["volume"] or 0),
             "suspended": r["volume"] is None}
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
              "type": t["type"], "trade_id": t["trade_id"], "volume": t["volume"]}
             for t in report.get("trade_log", []) if t["code"] == code]
    return {"code": code, "name": name, "bars": bars, "marks": marks}
