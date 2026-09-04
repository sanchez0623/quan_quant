# -*- coding: utf-8 -*-
"""多专家并行体检（Multi-Agent Panel，A/B 实验版）。

结构（翻译自 MultiAgentSystem 的 ConcurrentStrategy + 汇总层唯一开方原则）：
  回测报告
   ├─ 规则引擎 findings（调用方传入，两模式共用，保证公平）
   ├─ 科1 交易质量：切片 + [下钻工具] → 科室发现（禁开方）
   ├─ 科2 曲线与风险：同上 → 科室发现
   ├─ 科3 市场环境：同上 → 科室发现      ← 三科线程池并行
   ↓ 汇总层（唯一开方人）：科室发现 + 规则findings + 参数表
   → markdown 诊断 + json 建议块 → _extract_suggestions（同一套 clamp 幻觉护栏）

与单会话（analyzer.analyze_backtest）的对照点：
- 相同：规则 findings 输入、下钻工具集、clamp 护栏、验证闭环下游
- 不同：先分科聚焦再汇总，替代单会话一口气看完（广度 vs 注意力）

预算：每科轮次≤PANEL_LANE_ROUNDS(3)；汇总层不带工具（信息已足够，省成本）。
科室失败/降级不阻断：某科 LLMError 时该科标记 failed，汇总层继续。
"""
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import drilldown
from .analyzer import (_curve_features, _extract_suggestions, _market_context,
                       _param_schema_brief, _run_with_tools, _trade_samples,
                       _trade_stats)
from .provider import LLMError, chat

PANEL_LANE_ROUNDS = 3   # 每科下钻轮次上限

# ---------------- 科室定义 ----------------

LANE_ROLE_SUFFIX = (
    "\n\n输出要求（严格执行）：\n"
    "1. 输出 3~8 条编号发现，每条格式：`[标签] 一句话结论 —— 证据：<引用切片中的具体数字>`；\n"
    "2. 只输出你本科室职责范围内的发现，禁止输出参数建议/修改数值；\n"
    "3. 可调用下钻工具取证（query_trades / get_code_profile / get_market_context），"
    "证据足够即停止；没有问题就如实说「未见异常」并给出依据。"
)

LANES = [
    {"lane": "交易质量", "focus": (
        "你是交易质量科审查员，只负责交易行为层面：做T盈亏与胜率、止损效率、"
        "加仓/减仓贡献、单票盈亏集中度、交易类型结构、费用侵蚀。")},
    {"lane": "曲线与风险", "focus": (
        "你是资金曲线与风险科审查员，只负责净值层面：回撤深度与结构、月度收益分布、"
        "资金利用率/空仓期、出金覆盖。")},
    {"lane": "市场环境", "focus": (
        "你是市场环境科审查员，只负责行情归因：策略 vs 基准指数（超额收益）、"
        "月度对照、区分「策略弱」还是「行情好/差」。")},
]


def _lane_messages(lane: dict, report: dict, findings: list[dict]) -> list:
    """科室会话消息：职责 + 与该科相关的切片数据 + 规则 findings 中相关项。"""
    related = {
        "交易质量": ["T_NEG_PNL", "T_WIN_RATE_LOW", "ADD_DRAG", "CONCENTRATION",
                   "STOP_HEAVY", "HIGH_FEE", "WIN_RATE_LOW", "NO_TRADES"],
        "曲线与风险": ["DEEP_DD", "IDLE_CAPITAL", "LONG_FLAT", "WD_SHORTFALL",
                     "LOW_PROFIT_RATIO"],
        "市场环境": ["UNDERPERFORM_BENCH", "OVERFIT_WARN"],
    }.get(lane["lane"], [])
    my_findings = [f for f in findings if f.get("code") in related]
    parts = {
        "绩效指标": report.get("metrics"),
    }
    if lane["lane"] == "交易质量":
        parts["交易统计深化"] = _trade_stats(report)
        parts["交易明细采样"] = _trade_samples(report)
    elif lane["lane"] == "曲线与风险":
        parts["资金曲线特征点"] = _curve_features(report)
    else:
        parts["市场环境"] = _market_context(report)
    user = (f"回测报告切片（JSON，本科室相关）：\n"
            + json.dumps(parts, ensure_ascii=False, default=str))
    if my_findings:
        user += ("\n规则引擎已在本科室范围发现以下问题（请深挖成因与证据，"
                 "不要复述结论）：\n" + json.dumps(my_findings, ensure_ascii=False))
    system = lane["focus"] + LANE_ROLE_SUFFIX
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _run_lane(lane: dict, report: dict, findings: list[dict], profile: Optional[str],
              db_path: Optional[str], username: Optional[str],
              data_dir: Optional[str], key_db_path: Optional[str] = None) -> dict:
    """单科室：下钻循环（轮次≤PANEL_LANE_ROUNDS），失败标记 error。"""
    messages = _lane_messages(lane, report, findings)
    try:
        content, trace, meta = _run_with_tools(
            messages, profile, db_path, username, report,
            data_dir=data_dir, max_rounds=PANEL_LANE_ROUNDS,
            key_db_path=key_db_path)
        return {"lane": lane["lane"], "status": "ok", "content": content,
                "tokens": meta.get("tokens"), "elapsed": meta.get("elapsed"),
                "model": meta.get("model"), "tool_trace": trace}
    except LLMError as e:
        return {"lane": lane["lane"], "status": "failed", "content": "",
                "error": str(e), "tool_trace": []}


SYNTH_SYSTEM_PROMPT = (
    "你是主诊分析师。三个专科审查员已独立完成体检并输出发现，规则引擎也给出了"
    "客观诊断（findings）。你的任务：\n"
    "1. 用中文输出 markdown，仅包含：\n"
    "## 诊断解读（整合科室发现与 findings，交叉印证或指出矛盾，标注来源科室；"
    "不得发明科室发现之外的新问题）\n"
    "## 参数敏感性判断（结合参数重要性与参数表，若有）\n"
    "## 优化建议（编号列表，每条对应科室发现或 finding code，具体到参数与方向，"
    "调整量必须落在参数表 min/max 区间内）\n"
    "2. 你是唯一有权给出参数建议的角色；只建议有把握的调整，宁缺毋滥。\n"
    "3. 最后必须以一个 ```json 代码块作为全文结尾，格式严格为：\n"
    '{"params": {"参数名": 新值, ...}, "risk_config": {"字段名": 新值, ...}}\n'
    "params 键只能取自「策略参数表」，risk_config 键只能取自：max_position_pct_per_stock, "
    "max_total_position_pct, stop_loss_mode, stop_loss_pct, atr_period, atr_multiplier, "
    "take_profit_pct, trailing_stop_pct, max_drawdown_breaker, max_intraday_trades, "
    "max_holdings, cash_reserve_pct, atr_trail_mult, atr_cost_base, atr_trail_floor, "
    "adaptive, adaptive_trend_ma, adaptive_slope_n, adaptive_k_loose, adaptive_k_tight, "
    "adaptive_vol_n, adaptive_vol_hi, adaptive_vol_lo；\n"
    "其中 stop_loss_mode 可取 fixed/atr/trailing/atr_trailing，adaptive 可取 off/trend/vol，"
    "atr_cost_base 可取 first/wavg；\n"
    '若认为无需调整，输出 {"params": {}, "risk_config": {}}。'
)


def _synth_messages(report: dict, findings: list[dict],
                    lane_results: list[dict]) -> list:
    lanes_text = "\n\n".join(
        f"### 【{r['lane']}科】{'（该科失败，无输出）' if r['status'] != 'ok' else r['content']}"
        for r in lane_results)
    parts = {
        "绩效指标": report.get("metrics"),
        "规则引擎findings": findings,
        "策略参数表": _param_schema_brief(report),
    }
    user = (f"专科体检结果：\n{lanes_text}\n\n"
            "规则引擎与参数表（JSON）：\n"
            + json.dumps(parts, ensure_ascii=False, default=str))
    return [{"role": "system", "content": SYNTH_SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def run_panel_analysis(report: dict, profile: Optional[str] = None,
                       db_path: Optional[str] = None,
                       username: Optional[str] = None,
                       findings: Optional[list[dict]] = None,
                       data_dir: Optional[str] = None,
                       max_workers: int = 3,
                       key_db_path: Optional[str] = None) -> dict:
    """多专家并行体检：三科室并行 → 汇总层唯一开方。
    返回与 analyze_backtest 同构的
    {content, suggestions, diagnostics, tool_trace, lanes, model, tokens, elapsed, profile}。"""
    if findings is None:
        from . import diagnostics
        findings = diagnostics.diagnose(report)
    # ---- 阶段1：三科室并行（provider 为同步 httpx，用线程池） ----
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = [ex.submit(_run_lane, lane, report, findings, profile,
                          db_path, username, data_dir, key_db_path)
                for lane in LANES]
        lane_results = [f.result() for f in futs]
    # ---- 阶段2：汇总层唯一开方（不带工具） ----
    result = chat(profile, _synth_messages(report, findings, lane_results),
                  temperature=0.3, db_path=db_path, username=username,
                  key_db_path=key_db_path)
    content, suggestions = _extract_suggestions(result["content"], report)
    all_trace = [{"lane": r["lane"], **t} for r in lane_results for t in r["tool_trace"]]
    ok_lanes = [r["lane"] for r in lane_results if r["status"] == "ok"]
    lanes_note = "/".join(ok_lanes) or "无"
    content += f"\n\n> 🧑‍⚕️ 多专家体检：{lanes_note} 科室完成，汇总层出方。\n"
    tokens = (result.get("tokens") or 0) + sum(r.get("tokens") or 0 for r in lane_results)
    elapsed = (result.get("elapsed") or 0) + sum(r.get("elapsed") or 0 for r in lane_results)
    return {"content": content, "suggestions": suggestions,
            "diagnostics": findings, "tool_trace": all_trace,
            "lanes": lane_results, "model": result.get("model"),
            "tokens": tokens, "elapsed": elapsed, "profile": result.get("profile")}
