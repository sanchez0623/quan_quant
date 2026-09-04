# -*- coding: utf-8 -*-
"""AI 下钻工具（方案 A）：只读、纯函数、零 LLM——AI 分析的「按需取数菜单」。

解决「摘要太瘦 vs 全喂太大」的两难：静态摘要回答「整体怎么样」，
疑点由 LLM 通过本模块的工具对回测报告定向取证，每次只取几 KB。
- 工具实现在全量 trade_log / equity_curve / benchmark 上做确定性聚合（无幻觉空间）
- 返回体一律小而聚焦（分组统计 + 极端样本），单结果超长自动截断
- execute_tool 为唯一分发入口，未知工具/参数错误返回 {"error": ...}（不抛异常，
  让 LLM 能读到错误并自我修正）

使用纪律（预算护栏在 analyzer 的调用循环里）：下钻轮次上限、单轮工具数上限。
"""
import json
from typing import Optional

# ---- 预算护栏 ----
TOOL_RESULT_MAX_CHARS = 6000   # 单个工具结果截断上限（追加"已截断"标记）
GROUP_ROW_CAP = 40             # 分组统计最多返回组数
TRADE_LIST_CAP = 100           # 单票明细最多返回笔数
SAMPLE_CAP = 3                 # 极端样本每组条数
OVERLAY_POINT_CAP = 80         # 行情叠加最多点位数

# 工具月度默认粒度：YYYY-MM
_TOOLS_DESC = [
    {
        "type": "function",
        "function": {
            "name": "query_trades",
            "description": "按维度分组统计已平仓交易的盈亏（亏损组排前）。用于定位"
                           "「亏损集中在哪些月份/哪些票/哪些交易类型」。返回各组的"
                           "笔数/总盈亏/胜率 + 过滤后整体统计 + 极端样本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {"type": "string",
                                 "enum": ["month", "code", "type"],
                                 "description": "分组维度：month=按月 / code=按股票 / type=按交易类型"},
                    "code": {"type": "string", "description": "可选，只看某只股票（6位代码）"},
                    "trade_type": {"type": "string",
                                   "description": "可选，只看某交易类型（如 止损/清仓/加仓/做T卖出）"},
                    "month": {"type": "string", "description": "可选，只看某月（YYYY-MM）"},
                },
                "required": ["group_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_profile",
            "description": "单票全景：全部进出记录（时间/方向/类型/价格/盈亏/原因）+ "
                           "盈亏汇总 + 回测区间内该票周线收盘（若数据可用）。用于深挖"
                           "某只票的亏损/盈利成因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_context",
            "description": "区间市场环境：策略 vs 基准指数的月度收益对照、平均仓位占比、"
                           "最深回撤谷列表。用于区分「策略弱」还是「行情差」，以及检查"
                           "回撤期仓位行为。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_month": {"type": "string", "description": "可选，起始月（YYYY-MM，含）"},
                    "end_month": {"type": "string", "description": "可选，结束月（YYYY-MM，含）"},
                },
            },
        },
    },
]

TOOL_SCHEMAS = _TOOLS_DESC
TOOL_NAMES = {t["function"]["name"] for t in _TOOLS_DESC}


def _brief(t: dict) -> dict:
    return {k: t.get(k) for k in ("time", "code", "side", "type", "price",
                                  "volume", "pnl", "reason")}


def _closed_trades(report: dict) -> list[dict]:
    return [t for t in (report.get("trade_log") or []) if t.get("pnl") is not None]


def _cap(obj, max_chars: int = TOOL_RESULT_MAX_CHARS):
    """结果超长时截断（保 JSON 可解析：序列化后截断为纯文本标记）"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= max_chars:
        return obj
    return {"truncated": True,
            "note": f"结果超长已截断（{len(s)}>{max_chars}字符），请用更细的过滤条件缩小范围",
            "preview": s[:max_chars]}


def query_trades(report: dict, group_by: str = "month", code: Optional[str] = None,
                 trade_type: Optional[str] = None, month: Optional[str] = None,
                 limit: int = SAMPLE_CAP) -> dict:
    """已平仓交易按维度分组统计。亏损组排前（诊断价值最大）。"""
    key_fn = {"month": lambda t: (t.get("time") or "")[:7],
              "code": lambda t: str(t.get("code")),
              "type": lambda t: str(t.get("type"))}.get(group_by)
    if key_fn is None:
        return {"error": "group_by 需为 month / code / type 之一"}
    trades = _closed_trades(report)
    if code:
        trades = [t for t in trades if str(t.get("code")) == str(code)]
    if trade_type:
        trades = [t for t in trades if t.get("type") == trade_type]
    if month:
        trades = [t for t in trades if (t.get("time") or "")[:7] == month]
    groups: dict[str, dict] = {}
    for t in trades:
        g = groups.setdefault(key_fn(t) or "未知", {"n": 0, "pnl": 0.0, "wins": 0})
        g["n"] += 1
        g["pnl"] += float(t["pnl"])
        g["wins"] += 1 if t["pnl"] > 0 else 0
    rows = [{"group": k, "n": v["n"], "pnl": round(v["pnl"], 2),
             "win_rate": round(v["wins"] / v["n"], 4)}
            for k, v in groups.items()]
    rows.sort(key=lambda r: r["pnl"])  # 亏损组排前
    ranked = sorted(trades, key=lambda t: t["pnl"])
    return _cap({
        "overall": {"n": len(trades),
                    "pnl": round(sum(float(t["pnl"]) for t in trades), 2),
                    "win_rate": (round(sum(1 for t in trades if t["pnl"] > 0) / len(trades), 4)
                                 if trades else None)},
        "groups": rows[:GROUP_ROW_CAP],
        "n_groups": len(rows),
        "极端样本": {"亏损最多": [_brief(t) for t in ranked[:limit]],
                 "盈利最多": [_brief(t) for t in ranked[-limit:][::-1]]},
    })


def get_code_profile(report: dict, code: str, data_dir: Optional[str] = None) -> dict:
    """单票全景：全部进出记录 + 盈亏汇总 +（可选）回测区间周线收盘。"""
    code = str(code)
    trades = [t for t in (report.get("trade_log") or []) if str(t.get("code")) == code]
    if not trades:
        return {"error": f"回测交易记录中无 {code}（可用 query_trades(group_by='code') 查看有哪些票）"}
    closed = [t for t in trades if t.get("pnl") is not None]
    types: dict[str, int] = {}
    for t in trades:
        types[t.get("type") or "?"] = types.get(t.get("type") or "?", 0) + 1
    overlay = _weekly_overlay(code, trades, data_dir)
    return _cap({
        "code": code,
        "name": next((t.get("name") for t in trades if t.get("name")), code),
        "summary": {
            "n_trades": len(trades),
            "n_closed": len(closed),
            "closed_pnl": round(sum(float(t["pnl"]) for t in closed), 2) if closed else 0.0,
            "win_rate": (round(sum(1 for t in closed if t["pnl"] > 0) / len(closed), 4)
                         if closed else None),
            "types": types,
            "first_trade": trades[0].get("time"),
            "last_trade": trades[-1].get("time"),
        },
        "trades": [_brief(t) for t in trades[:TRADE_LIST_CAP]],
        "n_trades_total": len(trades),
        "周线收盘(回测区间)": overlay,
    })


def _weekly_overlay(code: str, trades: list[dict], data_dir: Optional[str]) -> Optional[list]:
    """该票在交易区间的周线收盘（每N个交易日取样）。数据不可用时返回 None。"""
    try:
        import polars as pl

        from ..data import store
        dates = sorted({(t.get("time") or "")[:10] for t in trades} - {""})
        if not dates:
            return None
        daily = store.read_daily([code], data_dir)
        if daily is None or not daily.height:
            return None
        sub = (daily.with_columns(pl.col("date").str.slice(0, 10).alias("_d"))
                    .filter((pl.col("_d") >= dates[0]) & (pl.col("_d") <= dates[-1]))
                    .sort("date"))
        if not sub.height:
            return None
        step = max(1, sub.height // OVERLAY_POINT_CAP)
        return [{"date": r["date"][:10], "close": round(float(r["close"]), 3)}
                for r in sub.to_dicts()[::step]]
    except Exception:  # noqa: BLE001  行情叠加是增强项，失败静默降级
        return None


def get_market_context(report: dict, start_month: Optional[str] = None,
                       end_month: Optional[str] = None) -> dict:
    """区间市场环境：策略 vs 基准月度对照 + 仓位占比 + 回撤谷列表。"""
    ec = [p for p in (report.get("equity_curve") or [])]
    bench = (report.get("benchmark") or {}).get("curve") or []
    if not ec:
        return {"error": "报告中无资金曲线"}
    if start_month:
        ec = [p for p in ec if p["date"][:7] >= start_month]
    if end_month:
        ec = [p for p in ec if p["date"][:7] <= end_month]
    if len(ec) < 2:
        return {"error": f"区间 {start_month}~{end_month} 内数据不足"}
    bench_by_day = {p["date"]: p["equity"] for p in bench}
    months: dict[str, dict] = {}
    for i, p in enumerate(ec):
        m = p["date"][:7]
        g = months.setdefault(m, {"first": p["equity"], "last": p["equity"],
                                  "bench_first": None, "bench_last": None,
                                  "ratio_sum": 0.0, "ratio_n": 0})
        g["last"] = p["equity"]
        be = bench_by_day.get(p["date"])
        if be is not None:
            g["bench_first"] = g["bench_first"] or be
            g["bench_last"] = be
        r = p.get("position_ratio")
        if r is not None:
            g["ratio_sum"] += r
            g["ratio_n"] += 1
    monthly = []
    for m in sorted(months):
        g = months[m]
        row = {"month": m,
               "strategy_ret": round(g["last"] / g["first"] - 1, 4) if g["first"] else None,
               "avg_position_ratio": (round(g["ratio_sum"] / g["ratio_n"], 4)
                                      if g["ratio_n"] else None)}
        if g["bench_first"]:
            row["bench_ret"] = round(g["bench_last"] / g["bench_first"] - 1, 4)
        monthly.append(row)
    period = {"start": ec[0]["date"][:10], "end": ec[-1]["date"][:10],
              "strategy_ret": round(ec[-1]["equity"] / ec[0]["equity"] - 1, 4)}
    if bench_by_day and bench_by_day.get(ec[0]["date"]) and bench_by_day.get(ec[-1]["date"]):
        period["bench_ret"] = round(bench_by_day[ec[-1]["date"]]
                                    / bench_by_day[ec[0]["date"]] - 1, 4)
    return _cap({"period": period, "monthly": monthly,
                 "回撤谷(最深前5)": _dd_episodes(ec)})


def _dd_episodes(ec: list[dict], top: int = 5) -> list[dict]:
    """回撤谷：连续 drawdown<0 的区段按深度取最深前 N。"""
    episodes: list[dict] = []
    cur: Optional[dict] = None
    for p in ec:
        dd = p.get("drawdown")
        if dd is None:
            continue
        if dd < 0:
            if cur is None:
                cur = {"start": p["date"][:10], "trough": p["date"][:10],
                       "depth": dd}
            elif dd < cur["depth"]:
                cur["depth"] = dd
                cur["trough"] = p["date"][:10]
            cur["end"] = p["date"][:10]
        else:
            if cur is not None:
                episodes.append(cur)
                cur = None
    if cur is not None:
        episodes.append(cur)
    episodes.sort(key=lambda e: e["depth"])
    return [{"start": e["start"], "trough": e["trough"], "end": e["end"],
             "depth": round(e["depth"], 4)} for e in episodes[:top]]


def execute_tool(name: Optional[str], args: dict, report: dict,
                 data_dir: Optional[str] = None) -> dict:
    """工具分发唯一入口：未知工具/参数异常一律返回 error（LLM 可读并自我修正）"""
    try:
        if name == "query_trades":
            return query_trades(report,
                                group_by=str(args.get("group_by") or "month"),
                                code=args.get("code"),
                                trade_type=args.get("trade_type"),
                                month=args.get("month"))
        if name == "get_code_profile":
            code = args.get("code")
            if not code:
                return {"error": "缺少必填参数 code"}
            return get_code_profile(report, code, data_dir=data_dir)
        if name == "get_market_context":
            return get_market_context(report, start_month=args.get("start_month"),
                                      end_month=args.get("end_month"))
        return {"error": f"未知工具: {name}（可用: {sorted(TOOL_NAMES)}）"}
    except Exception as e:  # noqa: BLE001  工具失败不让整个分析崩掉
        return {"error": f"工具执行失败: {e}"}
