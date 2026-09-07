# -*- coding: utf-8 -*-
"""参数敏感度扫描（方案 B Phase 3a）：对关键参数 ±20% 三点网格各跑一次回测，
产出**实测**敏感度表喂给 AI 分析——替代 LLM 对参数敏感性的猜测。

- 网格：当前值 / -20% / +20%（clamp 到 schema min/max；int 取整；categorical 跳过）
- 成本护栏：参数数 ≤4、每参数 ≤5 值、总回测次数 ≤20（超出 400 由端点校验）
- 结果结构：{"baseline": 当前值指标, "rows": [{param, value, metrics 摘要}], "skipped": [...]}
- 当前值点复用原报告 metrics（少跑一次回测）
"""
import copy
import json
from pathlib import Path
from typing import Optional


def auto_pick_params(report: dict, limit: int = 3) -> list[str]:
    """自动选扫描参数：寻优 param_importance top → 缺省取 param_schema 中
    靠前的数值参数（跳过 frozen/categorical）。"""
    from ..engine.strategies import REGISTRY
    cfg = report.get("config") or {}
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    if strategy is None:
        return []
    schema = {p["key"]: p for p in strategy.param_schema}
    picked: list[str] = []
    importance = report.get("_param_importance") or {}
    for k in sorted(importance, key=lambda x: float(importance[x]), reverse=True):
        if k in schema and schema[k].get("type") in ("int", "float"):
            picked.append(k)
        if len(picked) >= limit:
            return picked
    for p in strategy.param_schema:
        if (p["key"] not in picked and not p.get("frozen")
                and p.get("type") in ("int", "float")):
            picked.append(p["key"])
        if len(picked) >= limit:
            break
    return picked[:limit]


def _grid_values(report: dict, key: str) -> tuple[list, Optional[str]]:
    """参数的三点网格 [当前, -20%, +20%]（当前点复用原 metrics 不重跑，
    返回 (待回测值列表, skip_reason)；当前值非法/无 schema 时给 skip_reason）。"""
    from ..engine.strategies import REGISTRY
    cfg = report.get("config") or {}
    strategy = REGISTRY.get(cfg.get("strategy_id"))
    schema = {p["key"]: p for p in (strategy.param_schema if strategy else [])}
    s = schema.get(key)
    cur = (cfg.get("params") or {}).get(key)
    if s is None or s.get("type") not in ("int", "float"):
        return [], "参数不存在或非数值类型"
    if not isinstance(cur, (int, float)) or isinstance(cur, bool):
        return [], "当前值非数值"
    lo, hi = s.get("min"), s.get("max")
    out = []
    for mult in (0.8, 1.2):
        v = cur * mult
        if s.get("type") == "int":
            v = int(round(v))
        else:
            v = round(float(v), 4)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        if v != cur and v not in out:
            out.append(v)
    if not out:
        return [], "网格值与当前值重合（区间边界）"
    return out, None


_METRIC_KEYS = ("total_return", "annual_return", "max_drawdown", "sharpe",
                "calmar", "win_rate", "profit_loss_ratio", "total_trades")


def scan_params(report: dict, param_keys: list[str], data_dir: Optional[str] = None,
                progress_cb=None) -> dict:
    """执行扫描：对每个参数的网格值各跑一次回测，汇总实测敏感度表。
    回测失败的单点记 error 不中断整表。"""
    from ..engine import datafeed, runner
    base_cfg = report.get("config") or {}
    base_metrics = report.get("metrics") or {}
    baseline = {k: base_metrics.get(k) for k in _METRIC_KEYS}
    rows: list[dict] = []
    skipped = []
    plan: list[tuple[str, object]] = []
    for key in param_keys:
        values, reason = _grid_values(report, key)
        if reason:
            skipped.append({"param": key, "reason": reason})
            continue
        for v in values:
            plan.append((key, v))
    total = len(plan)
    done = 0
    try:
        for key, value in plan:
            cfg = copy.deepcopy(base_cfg)
            cfg.pop("task_id", None)
            cfg.setdefault("params", {})[key] = value
            try:
                r = runner.run_backtest(cfg, data_dir=data_dir)
                m = r.get("metrics") or {}
                rows.append({"param": key, "value": value,
                             **{k: m.get(k) for k in _METRIC_KEYS}})
            except Exception as e:  # noqa: BLE001  单点失败不中断
                rows.append({"param": key, "value": value, "error": str(e)[:150]})
            done += 1
            if progress_cb:
                progress_cb(done / max(1, total),
                            f"敏感度扫描 {done}/{total}（{key}={value}）")
    finally:
        datafeed.clear_cache()
    # 稳定性摘要：同一参数不同取值的指标离散度（AI 解读「动不动会崩」的依据）
    stability = {}
    for key in param_keys:
        pts = [r for r in rows if r["param"] == key and "error" not in r]
        rets = [p["total_return"] for p in pts if isinstance(p.get("total_return"), (int, float))]
        if len(rets) >= 2:
            spread = round(max(rets) - min(rets), 4)
            stability[key] = {"收益极差(最大-最小)": spread,
                              "判定": "敏感" if abs(spread) > 0.15 else "不敏感"}
    return {"baseline": baseline, "rows": rows, "skipped": skipped,
            "stability": stability, "n_backtests": len(rows)}


def load_latest_sensitivity(backtest_id: str, reports_dir: str,
                            db_path: Optional[str] = None) -> Optional[dict]:
    """取该回测最近一次成功的敏感度扫描结果（分析时自动附加）。"""
    from .. import db
    for t in db.list_tasks("ai_sensitivity", db_path):
        if t["status"] != "success":
            continue
        if (t.get("payload") or {}).get("backtest_id") != backtest_id:
            continue
        path = (t.get("payload") or {}).get("report_path") or db.get_report_path(
            t["task_id"], db_path)
        if path and Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None
