# -*- coding: utf-8 -*-
"""Optuna 寻优封装：SQLite study、样本内外 70/30、MedianPruner、参数重要性"""
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
}

METRICS = ("annual_return", "sharpe", "calmar", "total_return")


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


def run_optimize(task_id: str, config: dict, param_space: dict, n_trials: int, metric: str,
                 db_path: Optional[str] = None, data_dir: Optional[str] = None,
                 optuna_dir: Optional[str] = None,
                 progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    from . import config as app_config
    data_dir = data_dir or str(app_config.DATA_DIR)
    optuna_dir = optuna_dir or str(app_config.OPTUNA_DIR)
    metric = metric if metric in METRICS else "annual_return"
    Path(optuna_dir).mkdir(parents=True, exist_ok=True)

    split = _split_date(config, data_dir)
    if split is None:
        raise RuntimeError("数据量不足以做样本内外划分（需>=10个交易日）")

    def cb(p, m):
        if progress_cb:
            progress_cb(p, m)

    cb(1, "初始化 Optuna study...")
    storage = f"sqlite:///{Path(optuna_dir) / (task_id + '.db')}".replace("\\", "/")
    study = optuna.create_study(study_name=task_id, storage=storage,
                                direction="maximize", load_if_exists=True,
                                pruner=optuna.pruners.MedianPruner(n_warmup_steps=5))

    def suggest(trial: optuna.Trial) -> dict:
        out = {}
        for key, sp in (param_space or {}).items():
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

    def objective(trial: optuna.Trial) -> float:
        suggested = suggest(trial)
        cfg = _merged_config(config, suggested)
        cfg["end_date"] = split  # 样本内寻优
        # 中间值报告：逐只股票回测做 prune 参考（完成度>50%后启用剪枝参考）
        universe = list(cfg.get("universe") or [])
        probes = universe[:3] if len(universe) > 3 else universe
        partials = []
        for k, code in enumerate(probes):
            single = dict(cfg)
            single["universe"] = [code]
            r = runner.run_backtest(single, data_dir=data_dir)
            partials.append(_metric_value(r, metric))
            trial.report(sum(partials) / len(partials), k)
            if trial.should_prune():
                raise optuna.TrialPruned()
        r = runner.run_backtest(cfg, data_dir=data_dir)
        return _metric_value(r, metric)

    for i in range(n_trials):
        remaining = n_trials - i
        done = len([t for t in study.trials if t.state.is_finished()])
        cb(min(99.0, done / max(n_trials, 1) * 100), f"寻优中: trial {done + 1}")
        study.optimize(objective, n_trials=1)
        _ = remaining

    best = study.best_trial
    best_params = best.params
    cb(99, "样本外验证...")

    # best_params 样本内/样本外完整回测
    in_cfg = _merged_config(config, best_params)
    in_cfg["end_date"] = split
    in_report = runner.run_backtest(in_cfg, data_dir=data_dir)
    out_cfg = _merged_config(config, best_params)
    out_cfg["start_date"] = split
    out_report = runner.run_backtest(out_cfg, data_dir=data_dir)

    in_m, out_m = in_report["metrics"], out_report["metrics"]
    iv, ov = _metric_value(in_report, metric), _metric_value(out_report, metric)
    overfit = "low"
    if ov < iv * 0.5:
        overfit = "high"
    elif ov < iv * 0.8:
        overfit = "medium"

    param_importance = None
    try:
        completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if len(completed) >= 3 and len(param_space or {}) >= 1:
            imp = optuna.importance.get_param_importances(study)
            param_importance = {k: round(float(v), 4) for k, v in imp.items()}
    except Exception:  # noqa: BLE001
        param_importance = None

    trials = []
    for t in study.trials:
        val = round(t.value, 6) if t.value is not None else None
        trials.append({
            "number": t.number, "params": t.params,
            "value": val,
            "state": t.state.name.lower(),
            # objective 只在样本内跑：value 即样本内值；样本外仅对最优参数验证
            "in_sample_value": val,
            "out_sample_value": (round(ov, 6) if t.number == best.number else None),
            "duration": (round(t.duration.total_seconds(), 2) if t.duration else None),
        })

    cb(100, "寻优完成")
    return {
        "task_id": task_id,
        "metric": metric,
        "n_trials": n_trials,
        "best_params": best_params,
        "best_value": round(best.value, 6),
        "trials": trials,
        "param_importance": param_importance,
        "oos_validation": {
            "in_sample": {k: in_m.get(k) for k in ("annual_return", "max_drawdown", "sharpe")},
            "out_sample": {k: out_m.get(k) for k in ("annual_return", "max_drawdown", "sharpe")},
            "overfit_risk": overfit,
        },
        "split_date": split,
        "config": config,
        "param_space": param_space,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
