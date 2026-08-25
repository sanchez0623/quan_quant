# -*- coding: utf-8 -*-
"""回测报告 → prompt → LLM 分析结果(markdown)"""
import json
import random
from typing import Optional

from .provider import chat

SYSTEM_PROMPT = (
    "你是一位资深量化策略分析师。请基于用户提供的回测报告数据，用中文输出 markdown 格式的分析，"
    "包含以下部分：\n"
    "## 策略弱点诊断（如止损过紧、做T贡献为负、回撤过大等，指出具体证据）\n"
    "## 参数敏感性判断（结合参数重要性，若有）\n"
    "## 优化建议（编号列表，每条需具体到参数与调整方向，并说明理由）"
)


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
    """返回 {content, model, tokens, elapsed, profile}；未配置任何可用 key 抛 LLMError"""
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
    return chat(profile, [{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user_msg}],
                temperature=0.3, db_path=db_path, username=username)
