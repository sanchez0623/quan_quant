# -*- coding: utf-8 -*-
"""动态选股（universe_auto）+ momentum_core 测试。

覆盖：
- momentum_core.select_top：门槛/排序/RPS/候选域/无后视镜基准日
- 分段滚动重选：初始池走坏触发重选、段间现金衔接、trade_id 唯一、净值连续
- 空仓不硬买：初始池为空直接报错；中途全市场崩塌后空仓现金推进
- validate_backtest_config：universe_auto 的策略限制与参数校验

数据构造说明：使用微小噪声（0.0005）的确定性分段收益——噪声远小于
波动下限兜底（_vol_floor=0.005），崩溃保护（ret5 > 2×vol5≈2.24%）对
UP=0.3%/日的趋势股恒不触发，测试结果完全确定。
"""
import numpy as np
import polars as pl
import pytest
from fastapi import HTTPException

from app.api.backtests import validate_backtest_config
from app.data import store, synthetic
from app.engine import momentum_core as mc
from app.engine.runner import run_backtest

N_DAYS = 330
SEG_START = 200          # 回测开始（交易日序号）
UP = 0.003               # 趋势日收益（5日约1.5%，低于崩溃保护阈值≈2.24%）
DOWN = -0.006
FLAT = 0.0
NOISE = 0.0005


def _write_market(tmp_path, plans, n_days=N_DAYS, seed0=42):
    """构造全市场日线：plans = {code: [(起始日序, 结束日序, 日收益), ...]} 分段收益。
    seed0 供域测试选定「全票 MACD 金叉成立」的种子（固定种子->结果恒定）"""
    dates = synthetic.trade_dates(n_days)
    rows = []
    for ci, (code, segs) in enumerate(plans.items()):
        rng = np.random.default_rng(seed0 + ci)
        prev = 10.0
        for di, d in enumerate(dates):
            ret = next((r for (s, e, r) in segs if s <= di < e), FLAT)
            val = ret + float(rng.normal(0, NOISE))
            c = prev * (1 + val)
            rows.append({"code": code, "date": d,
                         "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                         "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                         "volume": 1_000_000, "amount": round(c * 1_000_000, 2)})
            prev = c
    store.write_daily(pl.DataFrame(rows), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": list(plans), "name": [f"股{c}" for c in plans],
        "st": [False] * len(plans), "list_date": ["20000101"] * len(plans)}),
        str(tmp_path))
    return dates


def _auto_cfg(start, end, **over):
    cfg = {
        "name": "auto-test", "strategy_id": "momentum_t",
        "params": {}, "risk_config": {},
        "universe": [], "universe_auto": True,
        "auto_idle_days": 3, "auto_top_x": 2, "auto_above_ma": 60,
        "auto_with_accel": False, "auto_min_rps": None,
        "start_date": start, "end_date": end, "period": "daily",
        "initial_capital": 1_000_000, "exclude_st": True,
    }
    cfg.update(over)
    return cfg


# ---------------- momentum_core 单测 ----------------

def test_select_top_gate_and_rank(tmp_path):
    """强涨股入选且分数排序最高；死叉/破位股被门槛挡掉；RPS 最强者≈100"""
    dates = synthetic.trade_dates(220)
    rows = []
    for ci, (code, drift) in enumerate((("600000", UP), ("000001", UP),
                                        ("600036", DOWN), ("000002", DOWN))):
        rng = np.random.default_rng(7 + ci)  # 各股独立噪声，避免分数并列
        prev = 10.0
        for d in dates:
            c = prev * (1 + drift + float(rng.normal(0, NOISE)))
            rows.append({"code": code, "date": d,
                         "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                         "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                         "volume": 1_000_000, "amount": 0.0})
            prev = c
    store.write_daily(pl.DataFrame(rows), str(tmp_path))
    mf = mc.market_features(data_dir=str(tmp_path), p=mc.pick_params(above_ma=60))
    as_of = mc.as_of_before(mf, dates[-5])
    assert as_of is not None and as_of < dates[-5], "基准日必须严格早于请求日（无后视镜）"
    picked = mc.select_top(mf, as_of, top_x=1)
    assert picked.height == 1
    assert picked["code"][0] in ("600000", "000001"), "top1 应为强涨股"
    assert picked["rps"][0] > 0.99, "全市场最强者 RPS 应≈100"
    # 候选域限定到死叉股：跌破均线恒不满足门槛 -> 空结果
    picked2 = mc.select_top(mf, as_of, top_x=5, domain={"600036", "000002"})
    assert picked2.height == 0, "死叉+破位股应被门槛全部挡掉"
    assert mc.next_after(mf, as_of) is not None
    # 门槛+排序的确定性：top2 恰为两只强涨股
    top2 = mc.select_top(mf, as_of, top_x=2)
    assert set(top2["code"].to_list()) == {"600000", "000001"}


# ---------------- 分段滚动重选 ----------------

def test_auto_segments_rollover_on_idle(tmp_path):
    """初始池(A组强涨)走坏 -> 空仓3日触发重选 -> 新池(B组)接管并交易。

    无后视镜断言：段2 基准日 ≥ 段1 触发日（T-1 语义；触发日无票过门槛时
    允许向后顺延到恢复日）；净值连续断言：日期严格递增、段边界无跳变。"""
    dates = _write_market(tmp_path, {
        "600000": [(0, 210, UP), (210, N_DAYS, DOWN)],     # A组：先强涨后崩
        "600036": [(0, 210, UP), (210, N_DAYS, DOWN)],
        "000001": [(210, N_DAYS, UP)],                      # B组：后半程启动
        "000002": [(210, N_DAYS, UP)],
    })
    cfg = _auto_cfg(dates[SEG_START], dates[N_DAYS - 1])
    report = run_backtest(cfg, data_dir=str(tmp_path))

    assert report["universe_auto"] is True
    segs = report["auto_segments"]
    assert len(segs) >= 2, f"应至少切出2段，实际 {len(segs)}"
    assert set(segs[0]["universe"]) == {"600000", "600036"}, "段1 应为A组强涨股"
    assert segs[0]["trigger_day"], "段1 应记录重选触发日"
    # 段2 基准日 = 段1 触发日或其后的恢复日（T-1 无后视镜），池子换成 B 组
    assert segs[1]["as_of"] >= segs[0]["trigger_day"]
    assert set(segs[1]["universe"]) == {"000001", "000002"}, "段2 应换入B组"
    # 段2 有真实交易（新池接管后建仓）
    seg2_trades = [t for t in report["trade_log"] if t.get("seg") == 2]
    assert any(t["type"] == "开仓" for t in seg2_trades), "段2 应有开仓交易"
    assert min(t["time"][:10] for t in seg2_trades) >= segs[1]["start"]
    # trade_id 全局唯一递增
    ids = [t["trade_id"] for t in report["trade_log"]]
    assert ids == sorted(set(ids)) and (not ids or ids[0] == 1)
    # 净值曲线：日期严格递增无重复（段衔接完整）
    eq_dates = [e["date"] for e in report["equity_curve"]]
    assert eq_dates == sorted(set(eq_dates)), "净值曲线日期应严格递增且不重复"
    # 段边界无跳变：相邻净值点变动 < 30%（正常日波动远小于此）
    vals = [e["equity"] for e in report["equity_curve"]]
    for a, b in zip(vals, vals[1:]):
        assert b / a < 1.3 and b / a > 0.7, f"净值跳变: {a} -> {b}"
    # 段1 触发日之后的旧池交易被丢弃（重选即退役）
    trig = segs[0]["trigger_day"]
    assert all(t["time"][:10] <= trig for t in report["trade_log"] if t.get("seg") == 1)


def test_auto_empty_market_raises(tmp_path):
    """全市场走熊 -> 基准日无票过门槛 -> 初始池为空直接报错（不硬凑池子）"""
    dates = _write_market(tmp_path, {
        "600000": [(0, N_DAYS, DOWN)],
        "000001": [(0, N_DAYS, DOWN)],
    })
    cfg = _auto_cfg(dates[SEG_START], dates[N_DAYS - 1])
    with pytest.raises(RuntimeError, match="初始池为空"):
        run_backtest(cfg, data_dir=str(tmp_path))


def test_auto_all_bear_keeps_cash(tmp_path):
    """初始池存在但中途全市场崩塌 -> 重选后空池 -> 空仓现金推进，绝不硬买"""
    dates = _write_market(tmp_path, {
        "600000": [(0, 215, UP), (215, N_DAYS, DOWN)],
        "600036": [(0, 215, UP), (215, N_DAYS, DOWN)],
        "000001": [(0, N_DAYS, DOWN)],
        "000002": [(0, N_DAYS, DOWN)],
    })
    cfg = _auto_cfg(dates[SEG_START], dates[N_DAYS - 1])
    report = run_backtest(cfg, data_dir=str(tmp_path))
    trig = report["auto_segments"][0]["trigger_day"]
    assert trig, "段1 应触发重选"
    # 触发日后旧池退役且全市场无票过门槛 -> 不应再有任何交易
    assert all(t["time"][:10] <= trig for t in report["trade_log"]), \
        "崩塌后不应有任何交易（空仓不硬买）"
    # 触发日之后净值 = 现金恒定（空池段现金推进）
    tail = [e["equity"] for e in report["equity_curve"] if e["date"] > trig]
    assert tail, "触发日之后应有净值点"
    assert all(abs(v - tail[0]) < 1e-6 for v in tail), "空池段净值应恒定"


# ---------------- 候选域（auto_index / auto_boards） ----------------

def _write_constituents(tmp_path, mapping: dict[str, list[str]]):
    rows = []
    for key, codes in mapping.items():
        for c in codes:
            rows.append({"index_key": key, "code": c, "name": f"股{c}",
                         "update_date": "2026-01-01", "snapshot_date": "2026-01-01"})
    store.write_index_constituents(pl.DataFrame(rows), str(tmp_path))


def test_auto_domain_boards_and_index(tmp_path):
    """板块域：仅创业板 -> 池子=300001；指数域并集 hs300+zz500 -> 三只；
    指数∩板块交集；空交集 -> 诚实报错（候选域内无票）。"""
    dates = _write_market(tmp_path, {
        "600000": [(0, N_DAYS, UP)],   # 主板
        "600036": [(0, N_DAYS, UP)],   # 主板
        "300001": [(0, N_DAYS, UP)],   # 创业板
        "000001": [(0, N_DAYS, UP)],   # 主板(深)
    }, seed0=70)  # 70：按本测试精确配置扫描选定的「全票基准日金叉成立」种子
    _write_constituents(tmp_path, {
        "hs300": ["600000", "600036"],
        "zz500": ["000001", "300001"],
    })
    start, end = dates[SEG_START], dates[N_DAYS - 1]

    # 板块域：仅创业板
    rep = run_backtest(_auto_cfg(start, end, auto_top_x=5, auto_boards=["chinext"]),
                       data_dir=str(tmp_path))
    assert set(rep["auto_segments"][0]["universe"]) == {"300001"}

    # 指数域并集：hs300 ∪ zz500 = 全部 4 只（top_x=5 全取）
    rep = run_backtest(_auto_cfg(start, end, auto_top_x=5,
                                 auto_index=["hs300", "zz500"]), data_dir=str(tmp_path))
    assert set(rep["auto_segments"][0]["universe"]) == {"600000", "600036", "000001", "300001"}

    # 指数 ∩ 板块：hs300 ∩ 创业板 = 空集 -> 初始池为空（诚实报错，不回退全市场）
    with pytest.raises(RuntimeError, match="初始池为空"):
        run_backtest(_auto_cfg(start, end, auto_index=["hs300"], auto_boards=["chinext"]),
                     data_dir=str(tmp_path))

    # 指数 ∩ 板块有交集：hs300 ∩ 主板 = 两只主板
    rep = run_backtest(_auto_cfg(start, end, auto_top_x=5,
                                 auto_index=["hs300"], auto_boards=["main"]),
                       data_dir=str(tmp_path))
    assert set(rep["auto_segments"][0]["universe"]) == {"600000", "600036"}


def test_validate_auto_domain_params():
    # 非法指数/板块被拦截
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(auto_index=["nasdaq"]))
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(auto_boards=["nyse"]))
    # 合法组合通过
    out = validate_backtest_config(_base_cfg(auto_index=["hs300", "zz500"],
                                             auto_boards=["main"]))
    assert out["auto_index"] == ["hs300", "zz500"]


# ---------------- validate 校验 ----------------

def _base_cfg(**over):
    cfg = {"name": "v", "strategy_id": "momentum_t", "params": {},
           "universe": [], "universe_auto": True,
           "start_date": "2025-01-01", "end_date": "2025-06-01", "period": "daily"}
    cfg.update(over)
    return cfg


def test_validate_auto_ok_and_reject():
    # 合法：momentum_t + auto + 空 universe
    out = validate_backtest_config(_base_cfg())
    assert out["universe_auto"] is True
    # 拦截：非动量策略
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(strategy_id="ma_cross"))
    # 拦截：auto 开启却手填 universe
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(universe=["600000"]))
    # 拦截：auto 关闭且 universe 为空
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(universe_auto=False))
    # 拦截：参数越界
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(auto_top_x=9999))
    with pytest.raises(HTTPException):
        validate_backtest_config(_base_cfg(auto_idle_days=0))
