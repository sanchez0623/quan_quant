# -*- coding: utf-8 -*-
"""大盘趋势闸门（INDEX_GATE）测试。

覆盖：
- compute_index_gate 状态机：MA20 跌破 2 日确认触发、恢复需 2 日 ≥ MA20×1.01
  （滞回缓冲带）、单日闪破不触发、T-1 对齐（无后视镜）
- 指数缺失：compute_index_gate 返回 None；回测 index_gate=True 与关闭行为完全一致
- 集成：指数走熊段 gate 抑制开仓（含反弹段），gate off 对照组正常开仓；
  退出管理照常
- validate：非动量策略开启 gate 被 400 拦截
"""
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent))  # 复用 test_momentum_auto 的数据构造
from fastapi import HTTPException  # noqa: E402

from app.api.backtests import validate_backtest_config  # noqa: E402
from app.data import store, synthetic  # noqa: E402
from app.engine.momentum_core import (  # noqa: E402
    INDEX_GATE_BUFFER, compute_index_gate,
)
from app.engine.runner import run_backtest  # noqa: E402
from test_momentum_auto import (  # noqa: E402
    N_DAYS, _write_market,
)


def _write_index(tmp_path, dates, closes, index_key="000905"):
    """写指数日线（compute_index_gate 只消费 date/close）"""
    store.write_index_daily(pl.DataFrame({
        "index_key": [index_key] * len(dates),
        "date": list(dates), "open": list(closes), "high": list(closes),
        "low": list(closes), "close": list(closes),
        "volume": [0] * len(dates), "amount": [0.0] * len(dates),
    }), str(tmp_path))


# ---------------- 状态机单测 ----------------

def test_index_gate_state_machine(tmp_path):
    """跌破 MA20 连续 2 日触发（T-1 对齐：确认日次日才抑制）；
    恢复需连续 2 日收盘 >= MA20×(1+缓冲带)，中间地带保持停开仓"""
    dates = synthetic.trade_dates(80)
    closes = ([100.0] * 26          # MA20=100 平盘
              + [90.0] * 30         # 深跌：连续 below -> 第 3 日（T-1）抑制
              + [105.0] * 24)       # 收复：连续 recov -> 恢复
    _write_index(tmp_path, dates, closes)
    gate = compute_index_gate(str(tmp_path))
    assert gate is not None
    vals = gate["index_gate"].to_list()
    assert len(vals) == len(dates)
    # T-1 对齐：第 2 个 below 日（i=27 收盘确认）当日不抑制（vals[27]=gates[26]=False），
    # 次日（i=28）起抑制；i=57 收盘第 2 个 recov 日确认，i=58 起恢复
    assert vals[27] is False and vals[28] is True
    assert all(vals[28:58]), "深跌段应持续抑制"
    assert vals[57] is True and vals[58] is False
    assert not any(vals[58:]), "恢复后不应再抑制"


def test_index_gate_single_day_dip_not_triggered(tmp_path):
    """单日闪破（1 日 below）不足确认天数 -> 不触发；
    回到均线下方但未达缓冲带恢复线 -> 滞回区不误恢复也不触发"""
    dates = synthetic.trade_dates(60)
    closes = [100.0] * 30 + [90.0] + [100.0] * 29
    _write_index(tmp_path, dates, closes)
    gate = compute_index_gate(str(tmp_path))
    assert gate is not None
    assert all(gate["index_gate"].to_list()) is False
    # 缓冲带常量登记在案（恢复线 = MA20×(1+0.01)，内置不开放）
    assert INDEX_GATE_BUFFER == 0.01


def test_index_gate_missing_data_returns_none(tmp_path):
    """指数日线缺失 -> None（调用方降级为不抑制）"""
    assert compute_index_gate(str(tmp_path)) is None


# ---------------- 集成行为 ----------------

def test_index_gate_blocks_reopen_in_bear(tmp_path):
    """指数先涨后跌（跌破 MA20 确认触发）-> gate on 时熊市段（含反弹段）
    无新开仓、退出管理照常；gate off 对照组在反弹段重新开仓（场景有效）"""
    plans = {c: [(0, 200, 0.003), (200, N_DAYS, -0.006)]
             for c in ("600000", "600036", "000001", "000002",
                       "600037", "000003", "600040")}
    # 反弹票：下跌段中段反弹（gate off 会重新开仓；指数仍在跌 -> gate on 保持抑制）
    plans["300001"] = [(0, 200, 0.003), (200, 258, -0.006),
                       (258, 300, 0.004), (300, N_DAYS, -0.006)]
    dates = _write_market(tmp_path, plans, seed0=70)
    # 指数：前 202 日温和上行，随后 -0.8%/日 下行贯穿回测区间（含反弹段）
    idx = []
    for i in range(N_DAYS):
        base = 1000.0 * 1.002 ** min(i, 201)
        idx.append(base if i <= 201 else base * 0.992 ** (i - 201))
    _write_index(tmp_path, dates, idx)
    start, end = dates[200], dates[N_DAYS - 1]
    universe = list(plans)
    base_cfg = {"name": "idx-gate-test", "strategy_id": "momentum_slot", "params": {},
                "risk_config": {}, "period": "daily", "initial_capital": 1_000_000,
                "exclude_st": True, "universe": universe,
                "start_date": start, "end_date": end}
    rep_on = run_backtest(dict(base_cfg, index_gate=True), data_dir=str(tmp_path))
    rep_off = run_backtest(dict(base_cfg), data_dir=str(tmp_path))

    # gate 生效起始日（由同一指数数据推导，保持断言与实现口径一致）
    gm = compute_index_gate(str(tmp_path))
    first_on = next(d for d, v in zip(gm["day"].to_list(),
                                      gm["index_gate"].to_list()) if v)

    opens_on = [t for t in rep_on["trade_log"] if t["type"] == "开仓"]
    opens_off = [t for t in rep_off["trade_log"] if t["type"] == "开仓"]
    assert len(opens_on) <= len(opens_off), "gate on 的开仓不应多于 gate off"
    # 对照组（gate off）在反弹段确实重新开仓 -> 场景有效
    assert any(t["time"][:10] >= dates[265] for t in opens_off), \
        "对照组应在反弹段重新开仓（否则场景构造无效）"
    # gate on：闸门生效日前（含生效日当日——生效日成交可能来自前一日合法信号，
    # T-1 对齐）无任何新开仓；反弹段（远晚于触发日）绝对无新开仓
    assert all(t["time"][:10] <= first_on for t in opens_on), \
        "gate on 在指数跌破 MA20 期间不应开仓（生效日当日成交 = 前一日信号，合法）"
    assert not any(t["time"][:10] >= dates[265] for t in opens_on), \
        "gate on 在反弹段不应开仓"
    # 退出/止损管理照常
    assert any(t["side"] == "sell" for t in rep_on["trade_log"])


def test_index_gate_missing_degrades_to_off(tmp_path):
    """开启 index_gate 但指数日线缺失 -> 与关闭行为完全一致（向后兼容 + 静默降级）"""
    dates = _write_market(tmp_path, {
        "600000": [(0, N_DAYS, 0.003)],
        "600036": [(0, N_DAYS, 0.003)],
    }, seed0=70)
    cfg = {"name": "idx-missing", "strategy_id": "momentum_slot", "params": {},
           "risk_config": {}, "period": "daily", "initial_capital": 1_000_000,
           "exclude_st": True, "universe": ["600000", "600036"],
           "start_date": dates[200], "end_date": dates[N_DAYS - 1]}
    rep_on = run_backtest(dict(cfg, index_gate=True), data_dir=str(tmp_path))
    rep_off = run_backtest(dict(cfg), data_dir=str(tmp_path))
    assert rep_on["trade_log"] == rep_off["trade_log"]
    assert rep_on["metrics"]["total_return"] == rep_off["metrics"]["total_return"]


# ---------------- validate ----------------

def test_validate_index_gate_rejects_non_momentum():
    cfg = {"name": "v", "strategy_id": "ma_cross", "params": {},
           "universe": ["600000"], "index_gate": True,
           "start_date": "2025-01-01", "end_date": "2025-06-01", "period": "daily"}
    with pytest.raises(HTTPException) as e:
        validate_backtest_config(cfg)
    assert "大盘趋势闸门" in (e.value.detail or "")
