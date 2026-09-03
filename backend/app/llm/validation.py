# -*- coding: utf-8 -*-
"""AI 建议自动验证闭环（OPTIMIZE_AND_AI_PLAN 方案 B4/B5 共享工具）。

- merge_suggestions：建议合并进原回测配置（任务内验证与前端「应用建议」
  未来共用同一份合并逻辑，保证口径一致）。
- run_validation_backtest：同区间同 universe 用建议参数在 ai worker 进程内
  重跑一次回测（不建任务、不落报告列表），跑完清 datafeed 缓存防内存驻留。
- compare_metrics：关键指标 A/B 对比 + 三值结论（改善/持平/恶化）。
- review_commentary：验证结果回喂 LLM 出二轮点评（≤300字，采纳/部分/放弃），
  best-effort——点评失败不影响验证结论入库。

边界：验证回测与原回测同引擎同数据，天然满足无后视镜；验证失败时
analysis 仍为 success（AI 不为回测失败背锅），validation.error 记录原因。
"""
import copy
from typing import Optional

# 参与对比的关键指标（max_drawdown 越接近 0 越好，单独处理）
_KEY_METRICS = ("total_return", "sharpe", "calmar", "win_rate",
                "profit_loss_ratio")
# 恶化显著阈值：总收益跌超 5pct 或回撤加深超 2pct 直接判「恶化」
_SIG_RETURN_TH = 0.05
_SIG_DD_TH = 0.02


def merge_suggestions(config: dict, suggestions: dict) -> dict:
    """把 AI 建议（params/risk_config）合并进原回测配置，返回新配置（深拷贝）。"""
    cfg = copy.deepcopy(config or {})
    sug = suggestions or {}
    params = dict(cfg.get("params") or {})
    params.update(sug.get("params") or {})
    risk = dict(cfg.get("risk_config") or {})
    risk.update(sug.get("risk_config") or {})
    cfg["params"] = params
    cfg["risk_config"] = risk
    return cfg


def _better(key: str, orig, new) -> Optional[bool]:
    """单指标对比：True=建议版更好，False=更差，None=无法比较或持平。"""
    if not isinstance(orig, (int, float)) or not isinstance(new, (int, float)):
        return None
    if new == orig:
        return None  # 持平不计入变好/变差
    if key == "max_drawdown":
        return new > orig  # 回撤浅者胜（负值，越接近0越好）
    return new > orig


def compare_metrics(orig_metrics: dict, new_metrics: dict) -> dict:
    """关键指标 A/B 对比 + verdict。返回
    {verdict, rows: [{key,label,orig,new,delta,better}], better:[], worse:[]}。"""
    orig_metrics = orig_metrics or {}
    new_metrics = new_metrics or {}
    rows = []
    for key in (*_KEY_METRICS, "max_drawdown"):
        orig, new = orig_metrics.get(key), new_metrics.get(key)
        if not isinstance(orig, (int, float)) or not isinstance(new, (int, float)):
            continue
        b = _better(key, orig, new)
        rows.append({"key": key, "orig": round(orig, 4), "new": round(new, 4),
                     "delta": round(new - orig, 4), "better": b})
    better = [r["key"] for r in rows if r["better"] is True]
    worse = [r["key"] for r in rows if r["better"] is False]
    # 显著恶化判定
    dr = (new_metrics.get("total_return"), orig_metrics.get("total_return"))
    dd = (new_metrics.get("max_drawdown"), orig_metrics.get("max_drawdown"))
    sig_return_drop = (isinstance(dr[0], (int, float)) and isinstance(dr[1], (int, float))
                       and dr[0] - dr[1] < -_SIG_RETURN_TH)
    sig_dd_deepen = (isinstance(dd[0], (int, float)) and isinstance(dd[1], (int, float))
                     and dd[0] - dd[1] < -_SIG_DD_TH)
    if sig_return_drop or (len(worse) >= 2 and len(better) <= 1):
        verdict = "恶化"
    elif sig_dd_deepen and not sig_return_drop and len(better) >= 2:
        verdict = "持平"  # 收益明显换回更深回撤 → 不算改善
    elif len(better) >= 2 and not sig_dd_deepen:
        verdict = "改善"
    else:
        verdict = "持平"
    return {"verdict": verdict, "rows": rows,
            "better": better, "worse": worse,
            "sig_return_drop": sig_return_drop, "sig_dd_deepen": sig_dd_deepen}


def run_validation_backtest(config: dict, suggestions: dict, orig_metrics: dict,
                            data_dir: Optional[str] = None) -> dict:
    """同区间用建议配置重跑一次回测并对比（进程内，不建任务）。
    orig_metrics 为原回测 metrics（对比基准）；回测失败抛异常由调用方兜底。"""
    from ..engine import datafeed, runner
    cfg = merge_suggestions(config, suggestions)
    cfg.pop("task_id", None)
    cfg["name"] = f"{config.get('name') or '回测'}-AI验证"
    diff = {}
    for k in ("params", "risk_config"):
        for pk, pv in (suggestions.get(k) or {}).items():
            old = (config.get(k) or {}).get(pk)
            diff[f"{k}.{pk}"] = {"old": old, "new": pv}
    try:
        report = runner.run_backtest(cfg, data_dir=data_dir)
    finally:
        datafeed.clear_cache()  # 防验证回测数据驻留常驻 worker 内存
    comparison = compare_metrics(orig_metrics, report.get("metrics"))
    return {"config_diff": diff,
            "metrics": {"orig": orig_metrics, "new": report.get("metrics")},
            "comparison": comparison}


REVIEW_SYSTEM_PROMPT = (
    "你是量化策略分析师。你此前对一份回测报告给出了参数优化建议，"
    "系统已用建议参数在同一区间重跑回测（A/B 对比）。请基于实测数据输出"
    "不超过 300 字的中文点评，包含三部分：\n"
    "1. 结论：采纳 / 部分采纳 / 放弃（一句话）；\n"
    "2. 证据：引用对比表中的具体数字（收益/回撤/夏普等的变化）；\n"
    "3. 下一步：若放弃或部分采纳，指出下一个最值得试的单一调整方向。\n"
    "只依据提供的数字，禁止编造。纯文本输出，不要 markdown 标题。"
)


def review_commentary(orig_report: dict, validation: dict, profile: Optional[str] = None,
                      db_path: Optional[str] = None,
                      username: Optional[str] = None) -> Optional[str]:
    """验证结果回喂 LLM 出二轮点评；任何失败返回 None（best-effort）。"""
    try:
        from .provider import chat
        m = validation.get("metrics") or {}
        comp = validation.get("comparison") or {}
        user_msg = (
            "原始回测关键指标：\n"
            f"{_brief_metrics(m.get('orig'))}\n"
            "建议参数回测（A/B）关键指标：\n"
            f"{_brief_metrics(m.get('new'))}\n"
            "逐项对比与结论：\n"
            f"{comp.get('verdict')}；变好：{comp.get('better')}；变差：{comp.get('worse')}\n"
            f"调整内容：{validation.get('config_diff')}\n"
            "请给出点评。"
        )
        result = chat(profile, [{"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg}],
                      temperature=0.3, db_path=db_path, username=username)
        return result["content"].strip()
    except Exception:  # noqa: BLE001  点评属增强项，任何失败静默降级
        return None


def _brief_metrics(m: Optional[dict]) -> str:
    if not m:
        return "（无）"
    keys = ("total_return", "annual_return", "max_drawdown", "sharpe", "calmar",
            "win_rate", "profit_loss_ratio", "total_trades", "t_pnl")
    return json.dumps({k: m[k] for k in keys if m.get(k) is not None},
                      ensure_ascii=False)
