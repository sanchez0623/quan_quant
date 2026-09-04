# -*- coding: utf-8 -*-
"""回测报告 -> 系统诊断 + 加厚数据 -> LLM 分析结果(markdown + 结构化参数建议)

方案 B3（OPTIMIZE_AND_AI_PLAN）：诊断由 diagnostics.py 纯规则引擎给出，
LLM 只做「解读 findings + 开方」，禁止发明 findings 之外的问题。
幻觉护栏在代码层：建议值按策略 param_schema 的 min/max/choices 强制 clamp，
未知键/无效枚举直接丢弃，与原值相同的无效建议剔除。

数据加厚（P0-1b）：在原 4 块输入外新增
- 市场环境（基准指数区间收益/超额收益/基准月度最好最差月份）
- 交易统计深化（单票盈亏集中度/做T与止损明细/资金利用率/费用占比/做T拒单原因）
- 策略参数表（param_schema 含 min/max/label，供 LLM 在合法区间内开方）
"""
import json
import random
import re
from typing import Optional

from ..engine.strategies import REGISTRY
from . import diagnostics, drilldown
from .provider import LLMError, chat

SYSTEM_PROMPT = (
    "你是一位资深量化策略分析师。系统已用规则引擎对回测报告完成了客观诊断"
    "（findings，含证据与建议方向），你的任务是：\n"
    "1. 用中文输出 markdown 分析，仅包含以下部分：\n"
    "## 诊断解读（逐条解读系统给出的 findings，结合数据解释成因；"
    "不得发明 findings 之外的新问题；若 findings 为空请分析策略为何健康）\n"
    "## 参数敏感性判断（结合参数重要性与参数表，若有）\n"
    "## 优化建议（编号列表，每条对应一个 finding code（如 [T_NEG_PNL]），"
    "具体到参数与调整方向、说明理由；调整量必须落在参数表 min/max 区间内）\n\n"
    "最后必须以一个 ```json 代码块作为全文结尾（系统会用它自动生成下一轮回测配置，"
    "并自动跑验证回测对比），格式严格为：\n"
    '{"params": {"参数名": 新值, ...}, "risk_config": {"字段名": 新值, ...}}\n'
    "要求：\n"
    "1. params 的键只能取自「策略参数表」中已有的参数名，只写需要调整的，"
    "数值类型与原值保持一致，且必须落在该参数的 min/max 区间内（越界会被系统剔除）；\n"
    "2. risk_config 的键只能取自：max_position_pct_per_stock, max_total_position_pct, stop_loss_mode, "
    "stop_loss_pct, atr_period, atr_multiplier, take_profit_pct, trailing_stop_pct, "
    "max_drawdown_breaker, max_intraday_trades, max_holdings, cash_reserve_pct, "
    "atr_trail_mult, atr_cost_base, atr_trail_floor, adaptive, adaptive_trend_ma, "
    "adaptive_slope_n, adaptive_k_loose, adaptive_k_tight, adaptive_vol_n, "
    "adaptive_vol_hi, adaptive_vol_lo；\n"
    "   其中 stop_loss_mode 可取 fixed / atr / trailing / atr_trailing；"
    "atr_trailing 表示止损线 = max(成本−k1×ATR, 最高价−k2×ATR) 且只上不下，"
    "k1 用 atr_multiplier、k2 用 atr_trail_mult；adaptive 可取 off / trend / vol，"
    "trend 表示按个股趋势（收盘价 vs 均线 + 均线斜率）自动缩放 k1/k2；\n"
    "3. 只建议有把握的调整，宁缺毋滥；若 findings 为空或认为无需调整，"
    '输出 {"params": {}, "risk_config": {}}。'
)

# risk_config 允许建议修改的字段（与 RiskConfigModel 对齐）
_RISK_FIELDS = {
    "max_position_pct_per_stock", "max_total_position_pct", "stop_loss_mode",
    "stop_loss_pct", "atr_period", "atr_multiplier", "take_profit_pct",
    "trailing_stop_pct", "max_drawdown_breaker", "max_intraday_trades",
    "max_holdings", "cash_reserve_pct",
    # atr_trailing
    "atr_trail_mult", "atr_cost_base", "atr_trail_floor",
    # 自适应止损
    "adaptive", "adaptive_trend_ma", "adaptive_slope_n", "adaptive_k_loose",
    "adaptive_k_tight", "adaptive_vol_n", "adaptive_vol_hi", "adaptive_vol_lo",
}

# risk_config 枚举字段合法值（越界丢弃）
_RISK_ENUMS = {
    "stop_loss_mode": {"fixed", "atr", "trailing", "atr_trailing"},
    "adaptive": {"off", "trend", "vol"},
    "atr_cost_base": {"first", "wavg"},
}

# ---- 数据下钻工具（方案 A）：预算护栏 ----
TOOL_SECTION = (
    "\n\n## 数据下钻工具（只读取证，按需使用）\n"
    "你可以调用以下工具对摘要/findings 中的疑点取证后再下结论：\n"
    "- query_trades(group_by=\"month\"|\"code\"|\"type\", code?, trade_type?, month?)："
    "已平仓交易按月/票/类型分组的盈亏统计（亏损组排前）+ 极端样本；\n"
    "- get_code_profile(code)：单票全部进出记录、盈亏汇总、回测区间周线收盘（若数据可用）；\n"
    "- get_market_context(start_month?, end_month?)：区间策略 vs 基准月度收益对照、"
    "平均仓位占比、最深回撤谷列表。\n"
    "使用纪律：先基于摘要与 findings 形成假设，仅对疑点下钻（总轮次≤6，"
    "不要用相同参数重复调用）；证据足够立即停止；"
    "无论是否用过工具，最终回答仍必须以 ```json 建议块结尾。"
)
MAX_TOOL_ROUNDS = 6            # 下钻轮次上限（每轮一次 LLM 调用）
MAX_TOOL_CALLS_TOTAL = 10      # 全程工具执行总次数上限
MAX_TOOL_CALLS_PER_ROUND = 4   # 单轮允许执行的工具数上限


def _run_with_tools(messages: list, profile: Optional[str], db_path: Optional[str],
                    username: Optional[str], report: dict,
                    data_dir: Optional[str]) -> tuple[str, list[dict], dict]:
    """下钻循环：LLM 请求工具 → 执行 → 结果回喂 → 直到给出最终回答。
    轮次/次数耗尽时强制一次无工具收尾。返回 (content, trace, 最后一次调用meta)。"""
    trace: list[dict] = []
    conv = list(messages)
    meta: dict = {}
    for _round in range(MAX_TOOL_ROUNDS):
        r = chat(profile, conv, temperature=0.3, db_path=db_path,
                 username=username, tools=drilldown.TOOL_SCHEMAS)
        meta = r
        tcs = [tc for tc in (r.get("tool_calls") or []) if isinstance(tc, dict)]
        if not tcs:
            return r["content"], trace, meta
        conv.append({"role": "assistant", "content": r.get("content") or None,
                     "tool_calls": r.get("tool_calls")})
        for tc in tcs[:MAX_TOOL_CALLS_PER_ROUND]:
            if len(trace) >= MAX_TOOL_CALLS_TOTAL:
                break
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            out = drilldown.execute_tool(fn.get("name"), args, report, data_dir=data_dir)
            trace.append({"name": fn.get("name") or "unknown", "args": args})
            conv.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": json.dumps(out, ensure_ascii=False)})
        if len(trace) >= MAX_TOOL_CALLS_TOTAL:
            break
    final = chat(profile, conv, temperature=0.3, db_path=db_path, username=username)
    return final["content"], trace, final


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
    suggestions = _sanitize_suggestions(data, report)
    # 从展示正文中移除 json 块（前端单独渲染建议卡片）
    content = (content[:last.start()] + content[last.end():]).rstrip() + "\n"
    return content, suggestions


def _sanitize_suggestions(data: dict, report: dict) -> Optional[dict]:
    """幻觉护栏（代码层，不靠 LLM 自觉）：
    - params：只留策略 param_schema 已有键；数值 clamp 到 [min, max]；int 取整；
      categorical 只留合法 choices（"value|标签" 只认 | 前的 value）；
      与原值相同的剔除（无效建议）。
    - risk_config：白名单字段；枚举字段校验；与原值相同的剔除。"""
    cfg = report.get("config") or {}
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    schema = {p["key"]: p for p in (strategy.param_schema if strategy else [])}
    cur_params = cfg.get("params") or {}
    cur_risk = cfg.get("risk_config") or {}
    def _same(a, b) -> bool:
        """数值/枚举同值判断（3 与 3.0 视为相同；bool 单独处理避免 int 混淆）"""
        try:
            if isinstance(a, bool) or isinstance(b, bool):
                return isinstance(a, bool) and isinstance(b, bool) and bool(a) == bool(b)
            return a == type(a)(b)
        except (TypeError, ValueError):
            return False

    clean_params: dict = {}
    for k, v in (data.get("params") or {}).items():
        k, s = str(k), schema.get(str(k))
        if v is None:
            continue
        try:
            if s is not None:
                # 已知策略：按 schema 类型/区间 clamp（幻觉护栏主路径）
                if s.get("type") == "int":
                    v = int(float(v))
                elif s.get("type") == "float":
                    v = float(v)
                elif s.get("type") == "categorical":
                    choices = [c.split("|")[0] for c in (s.get("choices") or [])]
                    v = str(v)
                    if choices and v not in choices:
                        continue
                else:
                    continue
                if isinstance(v, (int, float)):
                    if s.get("min") is not None:
                        v = max(s["min"], v)
                    if s.get("max") is not None:
                        v = min(s["max"], v)
                    if s.get("type") == "int":
                        v = int(v)
            elif not schema and k in cur_params and isinstance(v, (int, float, str, bool)):
                # 未知策略（历史报告无 strategy_id）：回退旧白名单口径
                # （config.params 键 + 基本类型），不做区间校验
                pass
            else:
                continue
        except (TypeError, ValueError):
            continue
        if k in cur_params and _same(cur_params[k], v):
            continue
        clean_params[k] = v

    clean_risk: dict = {}
    for k, v in (data.get("risk_config") or {}).items():
        k = str(k)
        if k not in _RISK_FIELDS or v is None:
            continue
        if k in _RISK_ENUMS:
            v = str(v)
            if v not in _RISK_ENUMS[k]:
                continue
        elif not isinstance(v, (int, float, bool)):
            continue  # 白名单内非枚举字段只接受数值/布尔
        if k in cur_risk and _same(cur_risk[k], v):
            continue
        clean_risk[k] = v
    if not clean_params and not clean_risk:
        return None
    return {"params": clean_params, "risk_config": clean_risk}


def _curve_features(report: dict) -> dict:
    curve = report.get("equity_curve") or []
    if not curve:
        return {}
    peak, max_dd, dd_start, dd_end = -1, 0.0, None, None
    cur_start = curve[0]["date"]
    for p in curve:
        eq = p["equity"]
        if eq > peak:
            peak = eq
            cur_start = p["date"]
        dd = eq / peak - 1 if peak > 0 else 0
        if dd < max_dd:
            max_dd, dd_start, dd_end = dd, cur_start, p["date"]
    # 最长回撤期（净值创新高间隔最长）
    peak, start_date = curve[0]["equity"], curve[0]["date"]
    longest, l_start, l_end = 0, None, None
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


def _market_context(report: dict) -> dict:
    """市场环境上下文：基准指数表现（区分「策略好」与「行情好」）。"""
    out: dict = {}
    m = report.get("metrics") or {}
    bench = report.get("benchmark") or {}
    if bench.get("name") or bench.get("index_key"):
        out["基准指数"] = bench.get("name") or bench.get("index_key")
        out["基准区间收益"] = m.get("benchmark_return")
        out["超额收益"] = m.get("excess_return")
        curve = bench.get("curve") or []
        if len(curve) >= 2:
            # 基准月度收益（等权近似：月首月末净值比）
            by_month: dict[str, list[float]] = {}
            for p in curve:
                by_month.setdefault(p["date"][:7], []).append(p["equity"])
            mret = [{"month": k, "return": round(v[-1] / v[0] - 1, 4)}
                    for k, v in by_month.items() if v and v[0] > 0]
            if mret:
                out["基准最好月份"] = sorted(mret, key=lambda x: x["return"],
                                          reverse=True)[:3]
                out["基准最差月份"] = sorted(mret, key=lambda x: x["return"])[:3]
    else:
        out["说明"] = "未启用基准对比（无法区分策略alpha与行情beta）"
    return out


def _trade_stats(report: dict) -> dict:
    """交易统计深化：集中度/做T与止损明细/资金利用率/费用占比/做T拒单原因。"""
    m = report.get("metrics") or {}
    log = report.get("trade_log") or []
    out: dict = {}
    # 单票盈亏集中度
    by_code: dict[str, dict] = {}
    for t in log:
        if t.get("pnl") is None:
            continue
        d = by_code.setdefault(t["code"], {"name": t.get("name") or t["code"],
                                           "pnl": 0.0, "n": 0})
        d["pnl"] += float(t["pnl"])
        d["n"] += 1
    if by_code:
        gross = sum(abs(d["pnl"]) for d in by_code.values())
        rows = sorted(by_code.items(), key=lambda kv: kv[1]["pnl"])
        worst = [{**d, "code": c, "pnl": round(d["pnl"], 2)} for c, d in rows[:5]]
        best = [{**d, "code": c, "pnl": round(d["pnl"], 2)}
                for c, d in reversed(rows[-5:])]
        out["单票盈亏"] = {
            "亏损最多5只": worst, "盈利最多5只": best,
            "盈亏集中度(最大|pnl|占比)": (round(max(abs(d["pnl"]) for d in by_code.values()) / gross, 4)
                                    if gross > 0 else None),
        }
    # 类型统计 + 止损/加仓盈亏
    type_count: dict[str, int] = {}
    for t in log:
        type_count[t["type"]] = type_count.get(t["type"], 0) + 1
    out["交易类型统计"] = type_count
    for key in ("stop_loss_pnl", "add_pnl", "reduce_pnl", "t_pnl", "t_pnl_closed"):
        if m.get(key) is not None:
            out[key] = m[key]
    # 资金利用率
    curve = report.get("equity_curve") or []
    ratios = [p["position_ratio"] for p in curve
              if p.get("position_ratio") is not None]
    if ratios:
        out["资金利用率"] = {"平均仓位占比": round(sum(ratios) / len(ratios), 4),
                          "最低仓位占比": round(min(ratios), 4)}
    # 费用占比
    fee, total_pnl, start_eq = (m.get("commission_total"), m.get("total_pnl"),
                                m.get("start_equity"))
    if isinstance(fee, (int, float)) and isinstance(total_pnl, (int, float)) \
            and total_pnl > 0:
        out["费用占净盈利比"] = round(fee / total_pnl, 4)
    elif isinstance(fee, (int, float)) and isinstance(start_eq, (int, float)) and start_eq:
        out["费用占初始资金比"] = round(fee / start_eq, 4)
    # 做T拒单原因（t_reject_events）
    rejects = report.get("t_reject_events") or []
    if rejects:
        rc: dict[str, int] = {}
        for r in rejects:
            rc[r.get("type") or r.get("reason") or "未知"] = \
                rc.get(r.get("type") or r.get("reason") or "未知", 0) + 1
        out["做T拒单统计"] = rc
    # 出金
    w = report.get("withdrawal") or {}
    if w:
        out["出金汇总"] = {k: w.get(k) for k in ("total", "t_profit", "topup",
                                               "shortfall", "recover") if w.get(k) is not None}
    return out


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


def _param_schema_brief(report: dict) -> list[dict]:
    """策略参数表（key/label/type/min/max/choices/default/current），
    供 LLM 在合法区间内开方；advanced/frozen 参数标注用途。"""
    cfg = report.get("config") or {}
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    if strategy is None:
        return []
    cur = cfg.get("params") or {}
    out = []
    for p in strategy.param_schema:
        out.append({"key": p["key"], "label": p.get("label"), "type": p.get("type"),
                    "min": p.get("min"), "max": p.get("max"),
                    "choices": p.get("choices"), "default": p.get("default"),
                    "current": cur.get(p["key"]),
                    "frozen": p.get("frozen", False)})
    return out


def analyze_backtest(report: dict, profile: Optional[str] = None,
                     db_path: Optional[str] = None,
                     param_importance: Optional[dict] = None,
                     username: Optional[str] = None,
                     findings: Optional[list[dict]] = None,
                     data_dir: Optional[str] = None) -> dict:
    """返回 {content, model, tokens, elapsed, profile, suggestions, diagnostics,
    tool_trace}；未配置任何可用 key 抛 LLMError。findings 缺省时用规则引擎现算。

    下钻循环：优先带工具调用（LLM 可对疑点只读取证）；端点不支持 tools
    （报错）时自动降级为单轮静态摘要分析，能力不缩水。"""
    if findings is None:
        findings = diagnostics.diagnose(report, param_importance)
    parts = {
        "回测配置": {k: report.get("config", {}).get(k)
                  for k in ("name", "strategy_id", "params", "risk_config",
                            "period", "universe", "start_date", "end_date",
                            "initial_capital")},
        "绩效指标": report.get("metrics"),
        "系统诊断findings(规则引擎)": findings,
        "资金曲线特征点": _curve_features(report),
        "市场环境": _market_context(report),
        "交易统计深化": _trade_stats(report),
        "交易明细采样": _trade_samples(report),
    }
    if param_importance:
        parts["参数重要性(来自寻优)"] = param_importance
    schema = _param_schema_brief(report)
    if schema:
        parts["策略参数表"] = schema
    user_msg = ("请分析以下回测报告（JSON，诊断已由系统规则引擎给出）：\n"
                + json.dumps(parts, ensure_ascii=False, default=str))
    base_messages = [{"role": "system", "content": SYSTEM_PROMPT + TOOL_SECTION},
                     {"role": "user", "content": user_msg}]
    try:
        content, tool_trace, meta = _run_with_tools(
            base_messages, profile, db_path, username, report, data_dir)
    except LLMError:
        # 端点不支持 function calling（或 key 全部不可用）→ 降级为单轮静态分析
        result = chat(profile, [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg}],
                      temperature=0.3, db_path=db_path, username=username)
        content, tool_trace, meta = result["content"], [], result
    content, suggestions = _extract_suggestions(content, report)
    if tool_trace:
        counts: dict[str, int] = {}
        for t in tool_trace:
            counts[t["name"]] = counts.get(t["name"], 0) + 1
        summary = "、".join(f"{k}×{v}" for k, v in counts.items())
        content += f"\n\n> 🔎 本分析共下钻取证 {len(tool_trace)} 次：{summary}\n"
    return {**meta, "content": content, "suggestions": suggestions,
            "diagnostics": findings, "tool_trace": tool_trace}
