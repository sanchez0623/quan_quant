# -*- coding: utf-8 -*-
"""回测报告 -> prompt -> LLM 分析结果(markdown + 结构化参数建议)"""
import json
import random
import re
from typing import Optional

from .provider import chat

SYSTEM_PROMPT = (
    "你是一位资深量化策略分析师。请基于用户提供的回测报告数据，用中文输出 markdown 格式的分析，"
    "包含以下部分：\n"
    "## 策略弱点诊断（如止损过紧、做T贡献为负、回撤过大等，指出具体证据）\n"
    "## 参数敏感性判断（结合参数重要性，若有）\n"
    "## 优化建议（编号列表，每条需具体到参数与调整方向，并说明理由）\n\n"
    "最后必须以一个 ```json 代码块作为全文结尾（系统会用它自动生成下一轮回测配置），格式严格为：\n"
    '{"params": {"参数名": 新值, ...}, "risk_config": {"字段名": 新值, ...}}\n'
    "要求：\n"
    "1. params 的键只能取自「回测配置.params」中已有的参数名，只写需要调整的，数值类型与原值保持一致；\n"
    "2. risk_config 的键只能取自：max_position_pct_per_stock, max_total_position_pct, stop_loss_mode, "
    "stop_loss_pct, atr_period, atr_multiplier, take_profit_pct, trailing_stop_pct, "
    "max_drawdown_breaker, max_intraday_trades, max_holdings, cash_reserve_pct；\n"
    '3. 若认为无需调整任何参数，输出 {"params": {}, "risk_config": {}}。'
)

# risk_config 允许建议修改的字段（与 RiskConfigModel 对齐）
_RISK_FIELDS = {
    "max_position_pct_per_stock", "max_total_position_pct", "stop_loss_mode",
    "stop_loss_pct", "atr_period", "atr_multiplier", "take_profit_pct",
    "trailing_stop_pct", "max_drawdown_breaker", "max_intraday_trades",
    "max_holdings", "cash_reserve_pct",
}


def _extract_suggestions(content: str, report: dict) -> tuple[str, Optional[dict]]:
    """提取 markdown 末尾的 ```json 建议块并净化。
    返回 (去掉json块的正文, 建议dict)；无有效建议时第二个返回值为 None。"""
    last = None
    for m in re.finditer(r"```json\s*(\{.*?\})\s*```", content, re.S):
        last = m
    if last is None:
        return content, None
    try:
        data = json.loads(last.group(1))
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return content, None
    known_params = set((report.get("config", {}).get("params") or {}).keys())
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    risk = data.get("risk_config") if isinstance(data.get("risk_config"), dict) else {}
    clean_params = {str(k): v for k, v in params.items()
                    if k in known_params and isinstance(v, (int, float, str, bool))}
    clean_risk = {str(k): v for k, v in risk.items()
                  if k in _RISK_FIELDS and isinstance(v, (int, float, str, bool))}
    suggestions = None
    if clean_params or clean_risk:
        suggestions = {"params": clean_params, "risk_config": clean_risk}
    # 从展示正文中移除 json 块（前端单独渲染建议卡片）
    content = (content[:last.start()] + content[last.end():]).rstrip() + "\n"
    return content, suggestions


def _curve_features(report: dict) -> dict:
    curve = report.get("equity_curve") or []
    if not curve:
        return {}
    peak, peak_i, max_dd, dd_start, dd_end = -1, -1, 0.0, None, None
    cur_start = curve[0]["date"]
    for i, p in enumerate(curve):
        eq = p["equity"]
        if eq > peak:
            peak = eq
            cur_start = p["date"]
        dd = eq / peak - 1 if peak > 0 else 0
        if dd < max_dd:
            max_dd, dd_start, dd_end = dd, cur_start, p["date"]
    # 最长回撤期（净值创新高间隔最长）
    peak = curve[0]["equity"]
    peak_date, longest, l_start, l_end = curve[0]["equity"], 0, None, None
    start_date = curve[0]["date"]
    for p in curve:
        if p["equity"] >= peak:
            span = _days_between(start_date, p["date"])
            if span > longest:
                longest, l_start, l_end = span, start_date, p["date"]
            peak = p["equity"]
            start_date = p["date"]
    monthly = report.get("monthly_returns") or []
    top_months = sorted(monthly, key=lambda m: m["return"], reverse=True)[:3]
    worst_months = sorted(monthly, key=lambda m: m["return"])[:3]
    return {
        "最大回撤区间": {"start": dd_start, "end": dd_end, "drawdown": round(max_dd, 4)},
        "最长回撤期": {"start": l_start, "end": l_end, "days": longest},
        "收益最好月份": top_months,
        "收益最差月份": worst_months,
    }


def _days_between(d1: str, d2: str) -> int:
    from datetime import datetime
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except ValueError:
        return 0


def _trade_samples(report: dict) -> dict:
    log = [t for t in (report.get("trade_log") or []) if t.get("pnl") is not None]
    wins = sorted(log, key=lambda t: t["pnl"], reverse=True)[:5]
    losses = sorted(log, key=lambda t: t["pnl"])[:5]
    rnd = random.Random(42).sample(log, min(10, len(log))) if log else []
    type_count: dict[str, int] = {}
    for t in report.get("trade_log") or []:
        type_count[t["type"]] = type_count.get(t["type"], 0) + 1

    def brief(t: dict) -> dict:
        return {k: t.get(k) for k in ("time", "code", "side", "type", "price",
                                      "volume", "pnl", "reason")}
    return {"盈利最多5笔": [brief(t) for t in wins],
            "亏损最多5笔": [brief(t) for t in losses],
            "随机10笔": [brief(t) for t in rnd],
            "交易类型统计": type_count}


def analyze_backtest(report: dict, profile: Optional[str] = None,
                     db_path: Optional[str] = None,
                     param_importance: Optional[dict] = None,
                     username: Optional[str] = None) -> dict:
    """返回 {content, model, tokens, elapsed, profile, suggestions}；未配置任何可用 key 抛 LLMError"""
    parts = {
        "回测配置": {k: report.get("config", {}).get(k)
                  for k in ("name", "strategy_id", "params", "risk_config",
                            "period", "universe", "start_date", "end_date",
                            "initial_capital")},
        "绩效指标": report.get("metrics"),
        "资金曲线特征点": _curve_features(report),
        "交易明细采样": _trade_samples(report),
    }
    if param_importance:
        parts["参数重要性(来自寻优)"] = param_importance
    user_msg = ("请分析以下回测报告（JSON）：\n"
                + json.dumps(parts, ensure_ascii=False, default=str))
    result = chat(profile, [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg}],
                  temperature=0.3, db_path=db_path, username=username)
    content, suggestions = _extract_suggestions(result["content"], report)
    return {**result, "content": content, "suggestions": suggestions}
