# -*- coding: utf-8 -*-
"""回测报告诊断引擎（OPTIMIZE_AND_AI_PLAN 方案 B2）：纯规则、零 LLM、可单测。

LLM 从「分析师」降级为「医生」——体检数值（findings）由本模块出，
LLM 只负责解读与开方，禁止发明 findings 之外的问题（幻觉护栏在 analyzer）。
所有数据取自回测报告既有字段（metrics/equity_curve/trade_log/benchmark），
阈值集中在模块常量，调优只动这里。
"""
from typing import Optional


# ---- 规则阈值（集中管理，便于调优） ----
T_NEG_MIN_TRADES = 5          # 做T为负规则的最小做T笔数
PLR_WIN_RATE_TH = 0.5         # 盈亏比规则：胜率高于此值才比较盈亏比
PLR_TH = 1.2                  # 盈亏比规则：低于此值判「止盈过早」
LOW_WIN_RATE_TH = 0.35        # 低胜率阈值
LOW_WIN_RATE_MIN_TRADES = 20  # 低胜率规则的最小交易笔数
DEEP_DD_TH = -0.25            # 深回撤阈值
IDLE_RATIO_TH = 0.2           # 资金闲置：平均仓位占比低于此值
LONG_FLAT_DAYS = 60           # 连续空仓交易日阈值
CONCENTRATION_TH = 0.6        # 单票盈亏集中度阈值
STOP_SHARE_TH = 0.4           # 止损占净亏损比例阈值
STOP_MIN_TRADES = 5           # 止损规则的最小止损笔数
FEE_SHARE_TH = 0.15           # 手续费占净盈利比例阈值
BENCH_UNDERPERFORM_TH = -0.05  # 跑输基准阈值（超额收益）
OVERFIT_IMPORTANCE_TH = 0.6   # 参数重要性过拟合预警阈值


def _finding(code: str, severity: str, title: str, evidence: str,
             hint: str) -> dict:
    return {"code": code, "severity": severity, "title": title,
            "evidence": evidence, "hint": hint}


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "-"


def _fmt_money(x: Optional[float]) -> str:
    return f"{x:,.0f}元" if isinstance(x, (int, float)) else "-"


def _curve_stats(report: dict) -> dict:
    """资金曲线衍生统计：平均仓位占比、最长连续空仓天数。"""
    curve = report.get("equity_curve") or []
    ratios = [p["position_ratio"] for p in curve
              if p.get("position_ratio") is not None]
    longest_flat, cur = 0, 0
    for p in curve:
        r = p.get("position_ratio")
        if r is not None and r <= 1e-6:
            cur += 1
            longest_flat = max(longest_flat, cur)
        else:
            cur = 0
    return {"avg_ratio": (sum(ratios) / len(ratios)) if ratios else None,
            "longest_flat": longest_flat if curve else 0}


def _code_concentration(report: dict) -> dict:
    """按票聚合已平仓盈亏：{code: {pnl, name}}，用于集中度诊断。"""
    by_code: dict[str, dict] = {}
    for t in report.get("trade_log") or []:
        if t.get("pnl") is None:
            continue
        d = by_code.setdefault(t["code"], {"pnl": 0.0, "name": t.get("name") or t["code"]})
        d["pnl"] += float(t["pnl"])
    return by_code


def diagnose(report: dict, param_importance: Optional[dict] = None) -> list[dict]:
    """对回测报告跑全部规则，返回 findings 列表（按严重度排序）。"""
    findings: list[dict] = []
    m = report.get("metrics") or {}
    total_pnl = m.get("total_pnl")
    total_trades = int(m.get("total_trades") or 0)

    # ---- NO_TRADES：零交易（门槛过高/数据缺失/区间过短） ----
    if total_trades == 0:
        findings.append(_finding(
            "NO_TRADES", "high", "回测区间内无任何成交",
            f"总交易笔数 0，期末权益 {_fmt_money(m.get('end_equity'))}",
            "检查选股门槛/动量参数是否过严、回测区间与股票池是否有效"))

    # ---- T_NEG_PNL：做T总贡献为负 ----
    t_count = int(m.get("t_trade_count") or 0)
    t_pnl = m.get("t_pnl")
    if t_count >= T_NEG_MIN_TRADES and isinstance(t_pnl, (int, float)) and t_pnl < 0:
        findings.append(_finding(
            "T_NEG_PNL", "high", "做T总贡献为负",
            f"做T {t_count} 笔，合计盈亏 {_fmt_money(t_pnl)}"
            + (f"，胜率 {_fmt_pct(m.get('t_win_rate'))}" if m.get("t_win_rate") is not None else ""),
            "做T网格阈值未能覆盖往返成本+滑点：考虑放宽 grid_atr_mult / t_ratio_base，"
            "或减少 max_t_times；若持续为负可评估关闭做T层"))

    # ---- T_WIN_RATE_LOW：做T胜率过低 ----
    t_wr = m.get("t_win_rate")
    if t_count >= 10 and isinstance(t_wr, (int, float)) and t_wr < 0.4:
        findings.append(_finding(
            "T_WIN_RATE_LOW", "medium", "做T胜率偏低",
            f"做T {t_count} 笔，胜率仅 {_fmt_pct(t_wr)}",
            "回补阈值/卖出阈值可能不对称：检查 asym_bias 类参数方向，"
            "或提高做T触发门槛"))

    # ---- LOW_PROFIT_RATIO：胜率高但盈亏比低（止盈过早） ----
    wr, plr = m.get("win_rate"), m.get("profit_loss_ratio")
    if (isinstance(wr, (int, float)) and isinstance(plr, (int, float))
            and wr > PLR_WIN_RATE_TH and plr < PLR_TH):
        findings.append(_finding(
            "LOW_PROFIT_RATIO", "medium", "胜率高但盈亏比不足",
            f"胜率 {_fmt_pct(wr)}，盈亏比仅 {plr:.2f}",
            "盈利单被过早了结：考虑放宽止盈/移动止损（trailing_stop_pct、"
            "atr_trail_mult ↑），让利润奔跑"))

    # ---- WIN_RATE_LOW：低胜率（入场判据松/止损慢） ----
    if (isinstance(wr, (int, float)) and total_trades >= LOW_WIN_RATE_MIN_TRADES
            and wr < LOW_WIN_RATE_TH):
        findings.append(_finding(
            "WIN_RATE_LOW", "medium", "交易胜率偏低",
            f"{total_trades} 笔交易胜率仅 {_fmt_pct(wr)}",
            "入场确认判据可能过松或止损过慢：检查趋势确认参数与止损倍数"))

    # ---- ADD_DRAG：加仓贡献为负 ----
    add_pnl = m.get("add_pnl")
    if isinstance(add_pnl, (int, float)) and add_pnl < 0:
        n_add = sum(1 for t in report.get("trade_log") or []
                    if t.get("type") == "加仓")
        n_open = sum(1 for t in report.get("trade_log") or []
                     if t.get("type") == "开仓")
        if n_add > max(2, n_open * 0.5):
            findings.append(_finding(
                "ADD_DRAG", "medium", "加仓拖累整体收益",
                f"加仓 {n_add} 笔（开仓 {n_open} 笔），加仓盈亏 {_fmt_money(add_pnl)}",
                "加仓多发生在趋势末端：考虑减小 add_scale（递减更快）、"
                "提高加仓触发门槛或减少 max_adds"))

    # ---- CONCENTRATION：单票盈亏过度集中 ----
    by_code = _code_concentration(report)
    if len(by_code) >= 3:
        gross = sum(abs(d["pnl"]) for d in by_code.values())
        if gross > 0:
            top_code, top_d = max(by_code.items(), key=lambda kv: abs(kv[1]["pnl"]))
            share = abs(top_d["pnl"]) / gross
            if share >= CONCENTRATION_TH:
                findings.append(_finding(
                    "CONCENTRATION", "medium", f"盈亏高度集中于 {top_code} {top_d['name']}",
                    f"{top_code} 贡献盈亏 {_fmt_money(top_d['pnl'])}，"
                    f"占全部已平仓盈亏绝对值的 {_fmt_pct(share)}",
                    "组合分散不足：考虑增大候选池/持仓只数（pool_n、max_holdings），"
                    "或检查该票是否踩雷"))

    # ---- DEEP_DD：深回撤 ----
    max_dd = m.get("max_drawdown")
    if isinstance(max_dd, (int, float)) and max_dd <= DEEP_DD_TH:
        findings.append(_finding(
            "DEEP_DD", "high", "最大回撤过深",
            f"最大回撤 {_fmt_pct(max_dd)}（阈值 {DEEP_DD_TH:.0%}）",
            "风控偏松：考虑收紧止损（atr_multiplier ↓）、降低单票仓位上限，"
            "或启用池级趋势开关（pool_gate）在弱市停开仓"))

    # ---- IDLE_CAPITAL / LONG_FLAT：资金利用率 ----
    cs = _curve_stats(report)
    if total_trades > 0:
        if cs["avg_ratio"] is not None and cs["avg_ratio"] < IDLE_RATIO_TH:
            findings.append(_finding(
                "IDLE_CAPITAL", "medium", "资金利用率偏低",
                f"全期平均仓位占比仅 {_fmt_pct(cs['avg_ratio'])}",
                "开仓信号不足：考虑放宽选股门槛（min_rps ↓、above_ma ↓）"
                "或提高满配比例（base_pct_max ↑）"))
        if cs["longest_flat"] >= LONG_FLAT_DAYS:
            findings.append(_finding(
                "LONG_FLAT", "medium", "长期空仓",
                f"最长连续空仓 {cs['longest_flat']} 个交易日",
                "动量门槛/趋势判据可能过严，或该区间市场环境与策略风格不匹配"))

    # ---- STOP_HEAVY：止损是主要亏损来源 ----
    stop_pnl = m.get("stop_loss_pnl")
    if (isinstance(stop_pnl, (int, float)) and stop_pnl < 0
            and isinstance(total_pnl, (int, float)) and total_pnl < 0
            and abs(stop_pnl) >= abs(total_pnl) * STOP_SHARE_TH):
        n_stop = sum(1 for t in report.get("trade_log") or []
                     if t.get("type") == "止损")
        if n_stop >= STOP_MIN_TRADES:
            findings.append(_finding(
                "STOP_HEAVY", "high", "止损是最大亏损来源",
                f"止损 {n_stop} 笔合计 {_fmt_money(stop_pnl)}，"
                f"占总亏损的 {_fmt_pct(abs(stop_pnl) / abs(total_pnl))}",
                "止损过紧导致频繁止损：考虑放宽 atr_multiplier / atr_trail_mult，"
                "或用自适应止损（adaptive=trend）在趋势市放宽"))

    # ---- HIGH_FEE：费用侵蚀 ----
    fee = m.get("commission_total")
    if isinstance(fee, (int, float)) and isinstance(total_pnl, (int, float)) \
            and total_pnl > 0 and fee >= total_pnl * FEE_SHARE_TH:
        findings.append(_finding(
            "HIGH_FEE", "medium", "交易费用侵蚀明显",
            f"手续费合计 {_fmt_money(fee)}，占净盈利的 {_fmt_pct(fee / total_pnl)}",
            "交易过于频繁：考虑提高做T触发门槛/减少日内次数，"
            "或加大持仓周期（降低换手）"))

    # ---- UNDERPERFORM_BENCH：跑输基准 ----
    excess = m.get("excess_return")
    if isinstance(excess, (int, float)) and excess <= BENCH_UNDERPERFORM_TH:
        bench = report.get("benchmark") or {}
        findings.append(_finding(
            "UNDERPERFORM_BENCH", "medium",
            f"跑输基准 {bench.get('name') or bench.get('index_key') or ''}",
            f"超额收益 {_fmt_pct(excess)}"
            + (f"（基准区间收益 {_fmt_pct(m.get('benchmark_return'))}）"
               if m.get("benchmark_return") is not None else ""),
            "区分「策略弱」与「行情好」：行情强势期跑输说明动量判据反应偏慢，"
            "考虑缩短动量周期或放宽入场确认"))

    # ---- WD_SHORTFALL：出金缺口 ----
    shortfall = m.get("shortfall_unrecovered")
    if isinstance(shortfall, (int, float)) and shortfall > 0:
        findings.append(_finding(
            "WD_SHORTFALL", "medium", "月度出金存在未补齐缺口",
            f"未补齐缺口合计 {_fmt_money(shortfall)}",
            "收益不足以支撑月度提取目标：降低 monthly_withdraw_base，"
            "或接受本金磨损（缺口由本金补齐）"))

    # ---- OVERFIT_WARN：寻优参数重要性过拟合信号 ----
    if param_importance:
        try:
            top_key, top_val = max(param_importance.items(),
                                   key=lambda kv: float(kv[1]))
            if float(top_val) > OVERFIT_IMPORTANCE_TH:
                findings.append(_finding(
                    "OVERFIT_WARN", "high",
                    f"参数 {top_key} 可能「记住行情」",
                    f"寻优参数重要性最高项 {top_key}={float(top_val):.2f}"
                    f"（阈值 {OVERFIT_IMPORTANCE_TH}）",
                    "该参数主导绩效→过拟合风险高：调参时优先在多窗口稳健性上验证，"
                    "避免单点最优"))
        except (TypeError, ValueError):
            pass

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return findings
