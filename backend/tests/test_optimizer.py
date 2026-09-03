# -*- coding: utf-8 -*-
"""方案A 寻优单元测试：多窗口目标、崩溃惩罚、默认参数集、分组坐标轮换端到端"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import synthetic
from app.optimizer import _defaults_flat, _window_metrics, _window_score, run_optimize


def _curve(seq):
    return [{"date": f"2026-01-{i + 1:02d}", "equity": v, "adjusted_equity": v}
            for i, v in enumerate(seq)]


def test_window_metrics_single_window_ret():
    w = _window_metrics(_curve([1.0, 1.1, 1.2, 1.3]), 1)
    assert len(w) == 1
    assert math.isclose(w[0]["ret"], 0.3, abs_tol=1e-9)
    assert math.isclose(w[0]["ann"], (1.3) ** (252 / 3) - 1, rel_tol=1e-6)


def test_window_metrics_slices_n_and_maxdd_nonpositive():
    w = _window_metrics(_curve([1.0, 1.1, 1.05, 1.2, 0.9, 1.3]), 3)
    assert len(w) == 3
    for win in w:
        assert win["maxdd"] <= 0


def test_window_score_variance_penalty_prefers_stable():
    # 平稳序列：两窗收益接近 -> 方差惩罚几乎为0
    stable = _window_metrics(_curve([1.0, 1.02, 1.04, 1.06, 1.08, 1.10]), 2)
    # 前段横盘后段暴涨：均值更高但窗口离散
    boom = _window_metrics(_curve([1.0, 1.0, 1.0, 1.0, 1.6, 1.6]), 2)
    s_stable = _window_score(stable, "total_return", 1.0, None)
    s_boom = _window_score(boom, "total_return", 1.0, None)
    assert s_stable > s_boom, "方差惩罚应压低'只在某段行情有效'的参数得分"


def test_window_score_dd_floor_penalty():
    c = _curve([1.0, 1.2, 1.2, 0.5, 0.6, 0.7])   # 第2窗内回撤 -58%
    w = _window_metrics(c, 3)
    s_no = _window_score(w, "total_return", 0.0, None)
    s_floor = _window_score(w, "total_return", 0.0, -0.40)
    assert s_floor < s_no, "击穿 dd_floor 的窗口应被大惩罚"


def test_defaults_flat_contains_params_and_risk():
    cfg = {"strategy_id": "ma_cross", "params": {"fast": 5},
           "risk_config": {"max_position_pct_per_stock": 30},
           "universe": ["600000"], "start_date": "2024-01-01", "end_date": "2030-12-31"}
    d = _defaults_flat(cfg)
    assert "fast" in d
    assert "max_position_pct_per_stock" in d


def test_run_optimize_resume_skips_completed(tmp_path):
    """断点续传：同一 task_id 重跑不重复执行已完成 trial"""
    synthetic.generate_demo_data(stocks=["600000", "000001", "600036"], days=160,
                                 data_dir=str(tmp_path), seed=7)
    cfg = {
        "name": "续传", "strategy_id": "ma_cross",
        "params": {"fast": 5, "slow": 10},
        "risk_config": {"max_position_pct_per_stock": 30},
        "universe": ["600000", "000001", "600036"],
        "start_date": "2024-01-01", "end_date": "2030-12-31",
        "period": "daily", "initial_capital": 1_000_000,
    }
    groups = [
        {"name": "快线", "n_trials": 2, "params": {"fast": {"type": "int", "low": 3, "high": 8}}},
        {"name": "慢线", "n_trials": 2, "params": {"slow": {"type": "int", "low": 12, "high": 30}}},
    ]
    objective = {"metric": "calmar", "n_windows": 3, "variance_penalty": 0.5, "dd_floor": -0.4}
    first = run_optimize("opt_resume", cfg, groups=groups, objective=objective, rounds=1,
                         data_dir=str(tmp_path), optuna_dir=str(tmp_path / "optuna"))
    n_first = len(first["trials"])
    assert n_first == 4  # 2 组 × 2 trial

    # 同一 task_id 重跑：应跳过已完成 trial，不产生重复
    second = run_optimize("opt_resume", cfg, groups=groups, objective=objective, rounds=1,
                          data_dir=str(tmp_path), optuna_dir=str(tmp_path / "optuna"))
    assert len(second["trials"]) == n_first, "续传不应重复执行已完成 trial"
    assert len(second["per_group_best"]) == 2
    # 最优结果一致（持久化 trial 被复用）
    assert second["best_params"] == first["best_params"]


def test_run_optimize_grouped_coordinate_rotation(tmp_path):
    """分组坐标轮换端到端：结构完整、rounds/per_group_best/objective 落位"""
    synthetic.generate_demo_data(stocks=["600000", "000001", "600036"], days=160,
                                 data_dir=str(tmp_path), seed=7)
    cfg = {
        "name": "分组寻优", "strategy_id": "ma_cross",
        "params": {"fast": 5, "slow": 10},
        "risk_config": {"max_position_pct_per_stock": 30},
        "universe": ["600000", "000001", "600036"],
        "start_date": "2024-01-01", "end_date": "2030-12-31",
        "period": "daily", "initial_capital": 1_000_000,
    }
    groups = [
        {"name": "快线", "n_trials": 2, "params": {"fast": {"type": "int", "low": 3, "high": 8}}},
        {"name": "慢线", "n_trials": 2, "params": {"slow": {"type": "int", "low": 12, "high": 30}}},
    ]
    objective = {"metric": "calmar", "n_windows": 3, "variance_penalty": 0.5, "dd_floor": -0.4}
    summary = run_optimize("opt_ut", cfg, groups=groups, objective=objective, rounds=2,
                           data_dir=str(tmp_path), optuna_dir=str(tmp_path / "optuna"))
    assert summary["metric"] == "calmar"
    assert summary["objective"]["n_windows"] >= 1
    assert len(summary["groups_schedule"]) == 2
    assert summary["rounds_history"] and summary["rounds_history"][0]["round"] == 1
    assert len(summary["per_group_best"]) >= 1
    assert "fast" in summary["best_params"] and "slow" in summary["best_params"]
    assert summary["oos_validation"]["overfit_risk"] in ("high", "medium", "low")
    assert "out_sample_windows" in summary["oos_validation"]
    assert all("group" in t and "round" in t for t in summary["trials"])
    # P0 护栏：稳健性验证结构存在
    assert "robustness" in summary
    assert summary["robustness"]["verdict"] in ("robust", "fragile", "unknown")


def test_walk_forward_folds_splits(tmp_path):
    """Walk-forward：样本内区间切出 n 个升序连续测试段"""
    from app.optimizer import _split_date, _walk_forward_folds
    synthetic.generate_demo_data(stocks=["600000"], days=150, data_dir=str(tmp_path), seed=3)
    cfg = {"strategy_id": "ma_cross", "params": {}, "risk_config": {},
           "universe": ["600000"], "start_date": "2024-01-01", "end_date": "2030-12-31"}
    split = _split_date(cfg, str(tmp_path))
    assert split, "应能切出 70% 分割日"
    folds = _walk_forward_folds(cfg, str(tmp_path), split, 3)
    assert len(folds) == 3
    starts = [f["test_start"] for f in folds]
    assert starts == sorted(starts), "测试段应时间升序"
    for f in folds:
        assert f["test_start"] <= f["test_end"] and f["n_test_days"] >= 2
    assert folds[-1]["test_end"] == split, "末折应覆盖到分割日"
    # 关闭（1折）不启用
    assert _walk_forward_folds(cfg, str(tmp_path), split, 1) == []


def test_run_optimize_walk_forward_result(tmp_path):
    """Walk-forward 端到端：结果带 walk_forward.enabled 与 folds"""
    synthetic.generate_demo_data(stocks=["600000", "000001", "600036"], days=200,
                                 data_dir=str(tmp_path), seed=11)
    cfg = {
        "name": "WF", "strategy_id": "ma_cross",
        "params": {"fast": 5, "slow": 10},
        "risk_config": {"max_position_pct_per_stock": 30},
        "universe": ["600000", "000001", "600036"],
        "start_date": "2024-01-01", "end_date": "2030-12-31",
        "period": "daily", "initial_capital": 1_000_000,
    }
    groups = [{"name": "快线", "n_trials": 2,
               "params": {"fast": {"type": "int", "low": 3, "high": 8}}}]
    objective = {"metric": "calmar", "n_windows": 2, "variance_penalty": 0.5,
                 "dd_floor": -0.4, "walk_forward_folds": 3}
    summary = run_optimize("opt_wf", cfg, groups=groups, objective=objective, rounds=1,
                           data_dir=str(tmp_path), optuna_dir=str(tmp_path / "optuna"))
    assert summary["walk_forward"]["enabled"] is True
    assert len(summary["walk_forward"]["folds"]) == 3
    assert summary["objective"]["walk_forward_folds"] == 3
    # 敏感度分析字段存在（OOS 段过短时可能为空列表，但不抛错）
    assert isinstance(summary.get("sensitivity"), list)


def test_oos_health_labels():
    """样本外绩效衰减：前涨后跌 -> poor；前后都涨 -> good"""
    from app.engine.stats import compute_oos_health
    # 前 40 日翻倍、后 40 日腰斩
    front = [1.0 * (1.02 ** i) for i in range(40)]
    back = [front[-1] * (0.985 ** i) for i in range(40)]
    c = [{"date": f"2026-01-{(i % 28) + 1:02d}", "equity": v, "adjusted_equity": v}
         for i, v in enumerate(front + back)]
    h = compute_oos_health(c)
    assert h is not None and h["label"] == "poor"
    assert h["front_ann"] > 0 > h["back_ann"]
    # 前后都涨 -> good
    all_up = [1.0 * (1.01 ** i) for i in range(80)]
    c2 = [{"date": f"2026-01-{(i % 28) + 1:02d}", "equity": v, "adjusted_equity": v}
          for i, v in enumerate(all_up)]
    h2 = compute_oos_health(c2)
    assert h2 is not None and h2["label"] == "good"
    # 数据不足 -> None
    assert compute_oos_health([{"date": "2026-01-01", "equity": 1.0}]) is None


def test_pool_candidates_scans_board(tmp_path):
    """候选池扫描：仅统计 300/301/688 且窗口内有足够 bar 的股票"""
    from app.optimizer import _pool_candidates
    synthetic.generate_demo_data(stocks=["600000", "000001", "600036", "300001", "688001"],
                                 days=120, data_dir=str(tmp_path), seed=1)
    gem, kcb = _pool_candidates(str(tmp_path), set(), "2024-01-01", "2026-12-31")
    assert "300001" in gem
    assert "688001" in kcb
    assert "600000" not in gem and "600000" not in kcb


def test_run_robustness_verdict_fragile(monkeypatch):
    """换池/换时段均差 -> fragile"""
    from app.optimizer import _run_robustness
    config = {"universe": ["300001"] * 12, "start_date": "2026-01-01", "end_date": "2026-08-26"}

    def fake_pool(*a, **k):
        gem = [f"300{i:03d}" for i in range(100, 112)]
        kcb = [f"688{i:03d}" for i in range(100, 112)]
        return gem, kcb

    def fake_metrics(_cfg, _d, _b, uni, start, _end):
        if uni[0].startswith("3000"):  # 原池（换时段）
            return {"annual_return": 0.08, "total_return": 0.08,
                    "max_drawdown": -0.15, "sharpe": 0.4}
        return {"annual_return": -0.15, "total_return": -0.10,
                "max_drawdown": -0.20, "sharpe": -0.6}

    monkeypatch.setattr("app.optimizer._pool_candidates", fake_pool)
    monkeypatch.setattr("app.optimizer._robust_metrics", fake_metrics)
    out = _run_robustness(config, "x", {})
    assert out["verdict"] == "fragile"
    assert len(out["cross_pool"]) == 2
    assert len(out["cross_period"]) == 2  # 2024、2025
    assert out["avg_annual_return_cross_pool"] < 0


def test_run_robustness_verdict_robust(monkeypatch):
    """换池/换时段均达标 -> robust"""
    from app.optimizer import _run_robustness
    config = {"universe": ["300001"] * 12, "start_date": "2024-01-01", "end_date": "2024-08-26"}

    def fake_pool(*a, **k):
        return ([f"300{i:03d}" for i in range(100, 112)],
                [f"688{i:03d}" for i in range(100, 112)])

    def fake_metrics(_cfg, _d, _b, _uni, _s, _e):
        return {"annual_return": 0.20, "total_return": 0.18,
                "max_drawdown": -0.12, "sharpe": 1.2}

    monkeypatch.setattr("app.optimizer._pool_candidates", fake_pool)
    monkeypatch.setattr("app.optimizer._robust_metrics", fake_metrics)
    out = _run_robustness(config, "x", {})
    assert out["verdict"] == "robust"
    assert "通过" in out["reason"]
