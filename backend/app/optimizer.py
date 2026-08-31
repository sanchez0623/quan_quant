# -*- coding: utf-8 -*-
"""Optuna 寻优封装：分层分组坐标轮换 + 多窗口稳健目标 + 样本外 70/30 验证。

方案（docs/OPTIMIZE_AND_AI_PLAN.md 方案 A）：
- 分组坐标轮换：rounds 轮 × 每组独立 Optuna study，其它组固定在当前最优，只搜本组
- 多窗口目标：每个 trial 只跑 1 次完整样本内回测，按权益曲线切 n 段算每窗指标，
  score = mean(每窗指标) - λ×std(每窗指标) - 大惩罚(任一窗回撤击穿 dd_floor)
- 向后兼容：旧格式（平铺 param_space + n_trials + metric）由 API 层包装为单组单窗
"""
import math
import time
from pathlib import Path
from typing import Callable, Optional

import optuna

from .engine import datafeed, runner
from .engine.strategies import apply_param_defaults

# risk_config 中的键（param_space 允许搜索这些键，落位到 risk_config）
RISK_KEYS = {
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

METRICS = ("annual_return", "sharpe", "calmar", "total_return")
MAX_TOTAL_TRIALS = 2000
EPS = 0.005  # 组间更新阈值（相对提升，防止坐标轮换震荡）

# metric -> 每窗指标列名
_WINDOW_COL = {"annual_return": "ann", "total_return": "ret",
               "sharpe": "sharpe", "calmar": "calmar"}


def _merged_config(base_config: dict, suggested: dict) -> dict:
    cfg = dict(base_config)
    params = dict(apply_param_defaults(cfg.get("strategy_id", ""), cfg.get("params") or {}))
    risk = dict(cfg.get("risk_config") or {})
    for k, v in suggested.items():
        if k in RISK_KEYS:
            risk[k] = v
        else:
            params[k] = v
    cfg["params"] = params
    cfg["risk_config"] = risk
    return cfg


def _split_date(backtest_config: dict, data_dir: str) -> Optional[str]:
    """时间轴前 70% 为样本内：返回分割日"""
    period = backtest_config.get("period", "daily")
    loader = datafeed.load_minute5 if period == "minute5" else datafeed.load_daily
    data = loader(list(backtest_config.get("universe") or []),
                  backtest_config.get("start_date"), backtest_config.get("end_date"), data_dir)
    all_dates = sorted({d for df in data.values() for d in
                        (df["date"].str.slice(0, 10).unique().to_list())})
    if len(all_dates) < 10:
        return None
    return all_dates[int(len(all_dates) * 0.7) - 1]


def _metric_value(report: dict, metric: str) -> float:
    v = report.get("metrics", {}).get(metric)
    if v is None:
        return -9e9
    return float(v)


def _suggest(space: dict, trial: optuna.Trial) -> dict:
    out = {}
    for key, sp in (space or {}).items():
        t = (sp or {}).get("type")
        if t == "int":
            out[key] = trial.suggest_int(key, int(sp["low"]), int(sp["high"]),
                                         step=int(sp.get("step", 1)))
        elif t == "float":
            out[key] = trial.suggest_float(key, float(sp["low"]), float(sp["high"]),
                                           step=sp.get("step"))
        elif t == "categorical":
            out[key] = trial.suggest_categorical(key, sp.get("choices", []))
        else:
            lo, hi = sp.get("low"), sp.get("high")
            if isinstance(lo, int) and isinstance(hi, int):
                out[key] = trial.suggest_int(key, lo, hi)
            else:
                out[key] = trial.suggest_float(key, float(lo), float(hi))
    return out


# ---------------- 多窗口目标 ----------------

def _window_metrics(equity_curve: list[dict], n_windows: int) -> list[dict]:
    """把整段权益曲线切成 n 个等长窗口，返回每窗 {ret, ann, sharpe, maxdd, calmar}"""
    n = len(equity_curve)
    if n < 2:
        return []
    edges = [int(i * n / n_windows) for i in range(n_windows)] + [n]
    out = []
    for w in range(n_windows):
        seg = equity_curve[edges[w]:edges[w + 1]]
        if len(seg) < 2:
            continue
        eq = [float(p.get("adjusted_equity") or p["equity"]) for p in seg]
        if eq[0] <= 0 or eq[-1] <= 0:
            continue
        ret = eq[-1] / eq[0] - 1
        days = len(eq) - 1
        ann = (1 + ret) ** (252.0 / days) - 1 if ret > -1 else -1.0
        peak = eq[0]
        maxdd = 0.0
        for e in eq:
            if e > peak:
                peak = e
            if peak > 0:
                maxdd = min(maxdd, e / peak - 1)
        daily = [eq[i + 1] / eq[i] - 1 for i in range(days) if eq[i] > 0]
        if len(daily) > 1:
            m = sum(daily) / len(daily)
            sd = math.sqrt(sum((d - m) ** 2 for d in daily) / (len(daily) - 1))
            sharpe = (m / sd * math.sqrt(252.0)) if sd > 0 else 0.0
        else:
            sharpe = 0.0
        calmar = ann / abs(maxdd) if maxdd < 0 else 0.0
        out.append({"ret": ret, "ann": ann, "sharpe": sharpe,
                    "maxdd": maxdd, "calmar": calmar})
    return out


def _window_score(windows: list[dict], metric: str, variance_penalty: float,
                  dd_floor: Optional[float]) -> float:
    """score = mean(每窗 metric) - λ×std(每窗 metric) - 大惩罚(任一窗回撤击穿)"""
    if not windows:
        return -9e9
    col = _WINDOW_COL.get(metric, "ann")
    vals = [w[col] for w in windows]
    mean_v = sum(vals) / len(vals)
    std_v = (sum((v - mean_v) ** 2 for v in vals) / len(vals)) ** 0.5
    score = mean_v - variance_penalty * std_v
    if dd_floor is not None and any(w["maxdd"] < dd_floor for w in windows):
        score -= 10.0
    return score


def _defaults_flat(config: dict) -> dict:
    """完整默认参数集（params + 风控），作为坐标轮换的起点与无改进时的回退"""
    base = _merged_config(config, {})
    out = dict(base.get("params") or {})
    for k, v in (base.get("risk_config") or {}).items():
        if k in RISK_KEYS:
            out[k] = v
    return out


def _single_score(report: dict, metric: str, dd_floor: Optional[float]) -> float:
    """单窗口（旧格式）目标：直接用报告的整段指标，保持向后兼容精确一致"""
    score = _metric_value(report, metric)
    if dd_floor is not None:
        mdd = report.get("metrics", {}).get("max_drawdown")
        if mdd is not None and float(mdd) < dd_floor:
            score -= 10.0
    return score


# ---------------- 跨池/跨时段稳健性验证（P0 护栏） ----------------

ROBUST_MIN_ANN = 0.10   # 换池/换时段平均年化阈值
ROBUST_MIN_SHARPE = 0.0  # 换池平均夏普阈值
ROBUST_ALT_MIN_BARS = 2000  # 换池候选股在目标窗口的最少 bar 数


def _pool_candidates(data_dir: str, exclude: set, start: str, end: str):
    """扫描 minute5 目录，返回 (创业板候选, 科创板候选) 两个按窗口内 bar 数降序的代码列表"""
    import polars as pl
    res_gem, res_kcb = [], []
    for f in Path(data_dir).glob("minute5/*.parquet"):
        code = f.stem
        if code in exclude or not code.startswith(("300", "301", "688")):
            continue
        try:
            day = pl.read_parquet(f, columns=["date"])["date"].str.slice(0, 10)
            n = day.filter((day >= start) & (day <= end)).len()
        except Exception:  # noqa: BLE001
            continue
        if n >= ROBUST_ALT_MIN_BARS:
            if code.startswith(("300", "301")):
                res_gem.append((code, n))
            elif code.startswith("688"):
                res_kcb.append((code, n))
    res_gem.sort(key=lambda x: (-x[1], x[0]))
    res_kcb.sort(key=lambda x: (-x[1], x[0]))
    return [c for c, _ in res_gem], [c for c, _ in res_kcb]


def _robust_metrics(config: dict, data_dir: str, best_params: dict,
                    universe: list, start: str, end: str):
    """用 best_params 跑一次回测，返回指标 dict；失败返回 None"""
    cfg = _merged_config(config, best_params)
    cfg["universe"] = universe
    cfg["start_date"] = start
    cfg["end_date"] = end
    try:
        r = runner.run_backtest(cfg, data_dir=data_dir)
        m = r.get("metrics", {})
        return {k: (float(m.get(k)) if m.get(k) is not None else None)
                for k in ("annual_return", "total_return", "max_drawdown", "sharpe")}
    except Exception:  # noqa: BLE001
        return None


def _run_robustness(config: dict, data_dir: str, best_params: dict) -> dict:
    """跨池/跨时段稳健性验证：换池(同窗口不同股票) + 换时段(同池不同年份)。
    返回结构：{cross_pool, cross_period, verdict, reason, ...}；数据不足时 verdict=unknown。
    """
    universe = list(config.get("universe") or [])
    start = str(config.get("start_date") or "")
    end = str(config.get("end_date") or "")
    if not universe or not start or not end:
        return {"verdict": "unknown", "reason": "缺少 universe/日期，跳过稳健性验证"}
    start_year = int(start[:4])
    alt_count = max(3, min(12, len(universe)))
    out: dict = {"cross_pool": [], "cross_period": [], "verdict": "unknown"}

    try:
        gem, kcb = _pool_candidates(data_dir, set(universe), start, end)
    except Exception as e:  # noqa: BLE001
        gem, kcb = [], []
        out["skipped"] = f"候选池扫描失败: {e}"

    for name, pool in (("创业板另{}只".format(min(len(gem), alt_count)), gem[:alt_count]),
                       ("科创板另{}只".format(min(len(kcb), alt_count)), kcb[:alt_count])):
        if len(pool) < 3:
            out["cross_pool"].append({"name": name, "universe": pool,
                                      "skipped": "候选不足(需>=3只)"})
            continue
        m = _robust_metrics(config, data_dir, best_params, pool, start, end)
        if m is None:
            out["cross_pool"].append({"name": name, "universe": pool, "skipped": "回测失败"})
        else:
            out["cross_pool"].append({"name": name, "universe": pool, **m})

    for y in (start_year - 2, start_year - 1):  # 最近两个完整年度
        if y < 2000:
            continue
        label = f"{y}全年"
        m = _robust_metrics(config, data_dir, best_params, universe, f"{y}-01-01", f"{y}-12-31")
        if m is None:
            out["cross_period"].append({"label": label, "skipped": "回测失败/无数据"})
        else:
            out["cross_period"].append({"label": label, **m})

    pools = [p for p in out["cross_pool"]
             if "skipped" not in p and p.get("annual_return") is not None]
    pers = [w for w in out["cross_period"]
            if "skipped" not in w and w.get("annual_return") is not None]
    if not pools or not pers:
        out["verdict"] = "unknown"
        out.setdefault("reason", "数据不足：换池或换时段样本缺失，无法判定稳健性")
        return out

    avg_pool_ann = sum(float(p["annual_return"]) for p in pools) / len(pools)
    avg_pool_shp = sum(float(p["sharpe"] or 0) for p in pools) / len(pools)
    avg_per_ann = sum(float(w["annual_return"]) for w in pers) / len(pers)
    out["avg_annual_return_cross_pool"] = round(avg_pool_ann, 6)
    out["avg_sharpe_cross_pool"] = round(avg_pool_shp, 6)
    out["avg_annual_return_cross_period"] = round(avg_per_ann, 6)
    robust = (avg_pool_ann >= ROBUST_MIN_ANN and avg_pool_shp >= ROBUST_MIN_SHARPE
              and avg_per_ann >= ROBUST_MIN_ANN)
    out["verdict"] = "robust" if robust else "fragile"
    out["reason"] = (f"换池平均年化 {avg_pool_ann:.1%}、平均夏普 {avg_pool_shp:.2f}；"
                     f"换时段平均年化 {avg_per_ann:.1%}。"
                     f"阈值：换池年化≥{ROBUST_MIN_ANN:.0%} 且夏普≥{ROBUST_MIN_SHARPE:.0f} "
                     f"且换时段年化≥{ROBUST_MIN_ANN:.0%}。"
                     + ("通过 → 结果在不同股票池/时段下保持有效" if robust
                        else "未通过 → 结果依赖特定股票池/时段，不建议实盘"))
    return out


# ---------------- 主流程 ----------------

def run_optimize(task_id: str, config: dict, *,
                 groups: list[dict], objective: dict, rounds: int = 1,
                 db_path: Optional[str] = None, data_dir: Optional[str] = None,
                 optuna_dir: Optional[str] = None,
                 progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    from . import config as app_config
    data_dir = data_dir or str(app_config.DATA_DIR)
    optuna_dir = optuna_dir or str(app_config.OPTUNA_DIR)
    Path(optuna_dir).mkdir(parents=True, exist_ok=True)

    metric = objective.get("metric") if objective.get("metric") in METRICS else "annual_return"
    n_windows_req = max(1, int(objective.get("n_windows") or 1))
    variance_penalty = float(objective.get("variance_penalty") or 0)
    dd_floor = objective.get("dd_floor")
    if dd_floor is not None:
        dd_floor = float(dd_floor)
    rounds = max(1, int(rounds or 1))
    if not groups:
        raise RuntimeError("groups 不能为空")

    split = _split_date(config, data_dir)
    if split is None:
        raise RuntimeError("数据量不足以做样本内外划分（需>=10个交易日）")

    def cb(p, m):
        if progress_cb:
            progress_cb(p, m)

    total_trials = rounds * sum(int(g.get("n_trials") or 0) for g in groups)

    # ---- 基线（默认参数）：用于自适应窗口数 + 坐标轮换的起点 ----
    cb(1, "评估基线（默认参数）...")
    base_cfg = _merged_config(config, {})
    base_cfg["end_date"] = split
    base_report = runner.run_backtest(base_cfg, data_dir=data_dir)
    in_days = len(base_report.get("equity_curve") or [])
    # 自适应窗口数：样本内交易日 // 30（下限1，上限请求值）
    n_windows = max(1, min(n_windows_req, in_days // 30)) if in_days >= 2 else 1
    if n_windows == 1:
        best_value = _single_score(base_report, metric, dd_floor)
    else:
        base_windows = _window_metrics(base_report.get("equity_curve") or [], n_windows)
        best_value = _window_score(base_windows, metric, variance_penalty, dd_floor)
    best_params: dict = _defaults_flat(config)
    cb(3, f"基线目标值 {best_value:.4f}（窗口数 {n_windows}，样本内 {in_days} 交易日）")

    storage = f"sqlite:///{Path(optuna_dir) / (task_id + '.db')}".replace("\\", "/")
    per_group_best: list[dict] = []
    rounds_history: list[dict] = []
    all_trials: list[dict] = []
    last_study: dict[int, optuna.Study] = {}
    done = 0

    def objective_fn(trial: optuna.Trial, g_space: dict) -> float:
        suggested = _suggest(g_space, trial)
        merged = {**best_params, **suggested}
        cfg = _merged_config(config, merged)
        cfg["end_date"] = split
        # 探针剪枝（保留）：前3只票单跑做 prune 参考
        universe = list(cfg.get("universe") or [])
        probes = universe[:3] if len(universe) > 3 else universe
        partials = []
        for k, code in enumerate(probes):
            single = dict(cfg)
            single["universe"] = [code]
            try:
                r = runner.run_backtest(single, data_dir=data_dir)
                partials.append(_metric_value(r, metric))
            except Exception:
                partials.append(-9e9)
            trial.report(sum(partials) / len(partials), k)
            if trial.should_prune():
                raise optuna.TrialPruned()
        r = runner.run_backtest(cfg, data_dir=data_dir)
        if n_windows == 1:
            return _single_score(r, metric, dd_floor)
        windows = _window_metrics(r.get("equity_curve") or [], n_windows)
        return _window_score(windows, metric, variance_penalty, dd_floor)

    for rnd in range(1, rounds + 1):
        improved = False
        round_entry = {"round": rnd, "best_value": round(best_value, 6), "groups": {}}
        for gi, group in enumerate(groups):
            gname = group.get("name") or f"组{gi + 1}"
            g_trials = int(group.get("n_trials") or 0)
            g_space = group.get("params") or {}
            if not g_space or g_trials <= 0:
                continue
            study_name = f"{task_id}__g{gi}__r{rnd}"
            study = optuna.create_study(study_name=study_name, storage=storage,
                                        direction="maximize", load_if_exists=True,
                                        pruner=optuna.pruners.MedianPruner(n_warmup_steps=5))
            last_study[gi] = study

            # 断点续传：跳过已完成 trial（load_if_exists 载入既有 study），
            # 只补跑剩余次数，进度把已有完成数计入
            existing = sum(1 for t in study.trials if t.state.is_finished())
            done += existing
            remaining = max(0, g_trials - existing)
            for i in range(remaining):
                done += 1
                pct = min(98.0, 3 + done / max(total_trials, 1) * 95)
                cb(pct, f"寻优中: 轮次 {rnd}/{rounds} · 组 {gi + 1}/{len(groups)}（{gname}）"
                        f" · trial {i + 1}/{g_trials}")
                study.optimize(lambda t, sp=g_space: objective_fn(t, sp), n_trials=1)

            gbest = study.best_trial
            g_value = float(gbest.value) if gbest.value is not None else -9e9
            g_params = dict(gbest.params)
            per_group_best.append({
                "group": gname, "round": rnd, "n_trials": g_trials,
                "best_value": round(g_value, 6), "params": g_params})
            round_entry["groups"][gname] = round(g_value, 6)
            if g_value > best_value + EPS:
                best_params.update(g_params)
                best_value = g_value
                improved = True

            for t in study.trials:
                all_trials.append({
                    "round": rnd, "group": gname, "number": t.number,
                    "params": t.params,
                    "value": (round(t.value, 6) if t.value is not None else None),
                    "state": t.state.name.lower(),
                })
        round_entry["improved"] = improved
        rounds_history.append(round_entry)
        if not improved:
            cb(96, f"第 {rnd} 轮无提升，提前收敛")
            break

    cb(97, "样本外验证...")

    # best_params 样本内/样本外完整回测
    in_cfg = _merged_config(config, best_params)
    in_cfg["end_date"] = split
    in_report = runner.run_backtest(in_cfg, data_dir=data_dir)
    out_cfg = _merged_config(config, best_params)
    out_cfg["start_date"] = split
    out_report = runner.run_backtest(out_cfg, data_dir=data_dir)

    in_m, out_m = in_report["metrics"], out_report["metrics"]
    iv = _metric_value(in_report, metric)
    ov = _metric_value(out_report, metric)
    overfit = "low"
    if ov < iv * 0.5:
        overfit = "high"
    elif ov < iv * 0.8:
        overfit = "medium"

    # 参数重要性：各组的最后 study 逐组合并（fANOVA -> PedAnova 兜底）
    param_importance = None
    try:
        imp: dict[str, float] = {}
        for gi, group in enumerate(groups):
            st = last_study.get(gi)
            if st is None or len(group.get("params") or {}) < 1:
                continue
            completed = [t for t in st.trials
                         if t.state == optuna.trial.TrialState.COMPLETE]
            if len(completed) < 3:
                continue
            try:
                gi_imp = optuna.importance.get_param_importances(st)
            except ImportError:
                gi_imp = optuna.importance.get_param_importances(
                    st, evaluator=optuna.importance.PedAnovaImportanceEvaluator())
            imp.update({k: round(float(v), 4) for k, v in gi_imp.items()})
        param_importance = imp or None
    except Exception:  # noqa: BLE001
        param_importance = None

    # 标记与最优参数一致的 trial 的样本外值
    for t in all_trials:
        t["in_sample_value"] = t["value"]
        t["out_sample_value"] = (round(ov, 6)
                                 if t["params"] == best_params else None)

    # P0 护栏：跨池/跨时段稳健性验证（失败不阻断任务）
    cb(98, "稳健性验证（换池/换时段）...")
    try:
        robustness = _run_robustness(config, data_dir, best_params)
    except Exception as e:  # noqa: BLE001
        robustness = {"verdict": "unknown", "reason": f"稳健性验证异常: {e}"}

    cb(100, "寻优完成")
    return {
        "task_id": task_id,
        "metric": metric,
        "n_trials": total_trials,
        "objective": {"metric": metric, "n_windows": n_windows,
                      "variance_penalty": variance_penalty, "dd_floor": dd_floor},
        "groups_schedule": [{"name": g.get("name"), "n_trials": g.get("n_trials"),
                             "params": g.get("params")} for g in groups],
        "rounds_history": rounds_history,
        "per_group_best": per_group_best,
        "best_params": best_params,
        "best_value": round(best_value, 6),
        "trials": all_trials,
        "param_importance": param_importance,
        "oos_validation": {
            "in_sample": {k: in_m.get(k) for k in ("annual_return", "max_drawdown", "sharpe")},
            "out_sample": {k: out_m.get(k) for k in ("annual_return", "max_drawdown", "sharpe")},
            "overfit_risk": overfit,
            "out_sample_windows": _window_metrics(
                out_report.get("equity_curve") or [], n_windows),
        },
        "robustness": robustness,
        "split_date": split,
        "config": config,
        "param_space": {k: v for g in groups for k, v in (g.get("params") or {}).items()},
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
