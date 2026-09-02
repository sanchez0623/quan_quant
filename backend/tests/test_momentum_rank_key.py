# -*- coding: utf-8 -*-
"""启动新鲜度排序键（RANK_KEY）测试。

覆盖：
- select_top 各 rank_key 的座次正确性（score/accel/fresh/mom_gap）
- 默认不传与非法值回退 score 旧行为；fresh 键 cross_days=null 沉底
- cross_days 金叉新鲜度真实路径：翻转日=0、次日=1、翻转前 None
- validate_backtest_config 对 auto_rank_key 的校验与端到端透传
"""
import numpy as np
import polars as pl
import pytest
from fastapi import HTTPException

from app.api.backtests import validate_backtest_config
from app.data import store, synthetic
from app.engine import momentum_core as mc
from app.engine.runner import run_backtest

# ---------------- 手造 MarketFeatures：座次规则 ----------------

_FEAT_SCHEMA = {
    "code": pl.Utf8, "day": pl.Utf8, "day_idx": pl.UInt32,
    "score": pl.Float64, "mom_gap": pl.Float64, "accel": pl.Float64,
    "cross_days": pl.UInt32, "macd_ok": pl.Boolean, "above": pl.Boolean,
}

# 四只票：score 降序与其余新鲜度键序刻意错开，保证各 rank_key 结果互不相同
_A = dict(code="300001", score=2.0, mom_gap=0.2, accel=0.5, cross_days=30)
_B = dict(code="300002", score=1.5, mom_gap=0.6, accel=0.9, cross_days=3)
_C = dict(code="300003", score=1.0, mom_gap=0.8, accel=0.7, cross_days=1)
_D = dict(code="300004", score=0.5, mom_gap=0.4, accel=0.6, cross_days=None)


def _fake_mf():
    rows = pl.DataFrame({
        "code": [t["code"] for t in (_A, _B, _C, _D)],
        "day": ["2026-06-01"] * 4,
        "day_idx": [0, 1, 2, 3],
        "score": [t["score"] for t in (_A, _B, _C, _D)],
        "mom_gap": [t["mom_gap"] for t in (_A, _B, _C, _D)],
        "accel": [t["accel"] for t in (_A, _B, _C, _D)],
        "cross_days": [t["cross_days"] for t in (_A, _B, _C, _D)],
        "macd_ok": [True] * 4,
        "above": [True] * 4,
    }, schema=_FEAT_SCHEMA)
    return mc.MarketFeatures(rows, ["2026-06-01"], mc.pick_params())


def test_rank_key_score_default_and_fallback():
    """score 键、默认不传、非法值三者座次一致（旧行为兼容）"""
    mf = _fake_mf()
    by_score = mc.select_top(mf, "2026-06-01", rank_key="score")["code"].to_list()
    by_default = mc.select_top(mf, "2026-06-01")["code"].to_list()
    by_bad = mc.select_top(mf, "2026-06-01", rank_key="__bad__")["code"].to_list()
    assert by_score == by_default == by_bad == ["300001", "300002", "300003", "300004"]
    # score 键返回列不含额外特征列
    assert mc.select_top(mf, "2026-06-01").columns == \
        ["rank", "code", "score", "rps"]


def test_rank_key_accel_and_mom_gap():
    """accel/mom_gap 键：主键降序 + 次键 score 降序，返回列含对应键值"""
    mf = _fake_mf()
    acc = mc.select_top(mf, "2026-06-01", rank_key="accel")
    assert acc["code"].to_list() == ["300002", "300003", "300004", "300001"]
    assert "accel" in acc.columns and acc["accel"][0] == 0.9
    gap = mc.select_top(mf, "2026-06-01", rank_key="mom_gap")
    assert gap["code"].to_list() == ["300003", "300002", "300004", "300001"]
    assert "mom_gap" in gap.columns


def test_rank_key_fresh_null_sinks_last():
    """fresh 键：cross_days 升序（刚金叉优先），无金叉记录 null 沉底"""
    mf = _fake_mf()
    fresh = mc.select_top(mf, "2026-06-01", rank_key="fresh")
    assert fresh["code"].to_list() == ["300003", "300002", "300001", "300004"]
    assert "cross_days" in fresh.columns
    assert fresh["cross_days"][-1] is None


def test_rank_key_top_x_cut():
    """top_x 截断在重排序之后生效（accel 键 top2 = accel 最大的两只）"""
    mf = _fake_mf()
    picked = mc.select_top(mf, "2026-06-01", top_x=2, rank_key="accel")
    assert picked["code"].to_list() == ["300002", "300003"]
    assert picked["rank"].to_list() == [1, 2]


# ---------------- cross_days 真实路径 ----------------

def test_cross_days_golden_cross(tmp_path):
    """先跌 150 日后涨：MACD 柱状图由负转正的翻转日 cross_days=0，
    次日=1，翻转前恒 None；验证特征列 mom_gap/accel 恒定输出"""
    dates = synthetic.trade_dates(300)
    rows = []
    prev = 10.0
    for di, d in enumerate(dates):
        ret = -0.006 if di < 150 else 0.004
        c = prev * (1 + ret)
        rows.append({"code": "600000", "date": d,
                     "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                     "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                     "volume": 1_000_000, "amount": 0.0})
        prev = c
    store.write_daily(pl.DataFrame(rows), str(tmp_path))
    mf = mc.market_features(data_dir=str(tmp_path), p=mc.pick_params(above_ma=60))
    assert {"mom_gap", "accel", "cross_days"} <= set(mf.feats.columns), \
        "market_features 应恒定输出新鲜度特征列"
    f = mf.feats.filter(pl.col("code") == "600000").sort("day")
    cds = f["cross_days"].to_list()
    # 长表已裁掉 dif/dea；macd_ok（dif>dea 且非空）的 False->True 翻转即金叉
    ok = f["macd_ok"].to_list()
    flips = [i for i in range(1, len(ok)) if ok[i] and not ok[i - 1]]
    assert flips, "先跌后涨序列应出现 MACD 金叉翻转（macd_ok 由 False 转 True）"
    k = flips[-1]
    assert cds[k] == 0, "金叉当日 cross_days 应为 0"
    assert cds[k + 1] == 1, "金叉次日 cross_days 应为 1"
    assert all(c is None for c in cds[:k]), "金叉前 cross_days 应为 None"


# ---------------- validate 校验与端到端 ----------------

def _base_cfg(**over):
    cfg = {"name": "v", "strategy_id": "momentum_t", "params": {},
           "universe": [], "universe_auto": True,
           "start_date": "2025-01-01", "end_date": "2025-06-01", "period": "daily"}
    cfg.update(over)
    return cfg


def test_validate_auto_rank_key():
    out = validate_backtest_config(_base_cfg(auto_rank_key="fresh"))
    assert out["auto_rank_key"] == "fresh"
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(auto_rank_key="bad"))


def test_auto_segments_with_rank_key(tmp_path):
    """端到端：auto_rank_key='fresh' 透传到 select_top，
    A 组强涨走坏 -> 重选 -> B 组接管（排序键变化不破坏段结构）"""
    n = 330
    dates = synthetic.trade_dates(n)
    plans = {
        "600000": [(0, 210, 0.003), (210, n, -0.006)],
        "600036": [(0, 210, 0.003), (210, n, -0.006)],
        "000001": [(210, n, 0.003)],
        "000002": [(210, n, 0.003)],
    }
    rows = []
    for ci, (code, segs) in enumerate(plans.items()):
        rng = np.random.default_rng(42 + ci)
        prev = 10.0
        for di, d in enumerate(dates):
            ret = next((r for (s, e, r) in segs if s <= di < e), 0.0)
            c = prev * (1 + ret + float(rng.normal(0, 0.0005)))
            rows.append({"code": code, "date": d,
                         "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                         "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                         "volume": 1_000_000, "amount": 0.0})
            prev = c
    store.write_daily(pl.DataFrame(rows), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": list(plans), "name": [f"股{c}" for c in plans],
        "st": [False] * len(plans), "list_date": ["20000101"] * len(plans)}),
        str(tmp_path))
    cfg = {"name": "rk", "strategy_id": "momentum_t", "params": {},
           "risk_config": {}, "universe": [], "universe_auto": True,
           "auto_idle_days": 3, "auto_top_x": 2, "auto_above_ma": 60,
           "auto_rank_key": "fresh",
           "start_date": dates[200], "end_date": dates[n - 1],
           "period": "daily", "initial_capital": 1_000_000, "exclude_st": True}
    report = run_backtest(cfg, data_dir=str(tmp_path))
    segs = report["auto_segments"]
    assert len(segs) >= 2, f"应至少切出2段，实际 {len(segs)}"
    assert set(segs[0]["universe"]) == {"600000", "600036"}
    assert set(segs[1]["universe"]) == {"000001", "000002"}
