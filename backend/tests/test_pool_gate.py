# -*- coding: utf-8 -*-
"""池级趋势开关（POOL_GATE）测试。

覆盖：
- _pool_gate_map 状态机：双阈值滞回、确认天数、T-1 对齐（无后视镜）
- 集成：gate 开启后下跌段不再产生新开仓（环境税被阻断）；gate 关闭与
  未配置行为完全一致（向后兼容）
- validate：非动量策略开启 gate 被 400 拦截
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # 复用 test_momentum_auto 的数据构造
from fastapi import HTTPException  # noqa: E402

from app.api.backtests import validate_backtest_config  # noqa: E402
from app.engine.momentum_core import (  # noqa: E402
    POOL_GATE_CONFIRM_DAYS, _pool_gate_map,
)
from app.engine.runner import run_backtest  # noqa: E402
from test_momentum_auto import (  # noqa: E402
    N_DAYS, _auto_cfg, _write_market,
)

# ---------------- 状态机单测 ----------------

def test_pool_gate_map_hysteresis_and_t1():
    """触发需连续2日低于线、恢复需连续2日达 2×线、输出已 T-1 对齐"""
    seq = [("d0", 0.5), ("d1", 0.05), ("d2", 0.05),
           ("d3", 0.5), ("d4", 0.5), ("d5", 0.5)]
    m = _pool_gate_map(seq, enter_th=0.15)
    # gates(收盘后): [F, F, T, T, F, F]；T-1 对齐：out[i] = gates[i-1]
    assert m["d0"] is False
    assert m["d1"] is False and m["d2"] is False, "确认期内不应提前停"
    assert m["d3"] is True and m["d4"] is True, "连续2日低 -> 第3日起停开仓（T-1）"
    assert m["d5"] is False, "连续2日达恢复线(0.3) -> 恢复"


def test_pool_gate_map_hysteresis_zone_keeps_state():
    """中间地带（enter ~ 2×enter）保持现状：触发后不因离开触发线立即恢复"""
    seq = [("d0", 0.5), ("d1", 0.05), ("d2", 0.05),
           ("d3", 0.20), ("d4", 0.20), ("d5", 0.20), ("d6", 0.20)]
    m = _pool_gate_map(seq, enter_th=0.15)
    assert m["d3"] is True  # 0.20 在滞回区 [0.15, 0.30)：不恢复
    assert m["d6"] is True, "滞回区内应保持停开仓状态"


def test_pool_gate_map_single_day_spike_not_triggered():
    """单日闪崩（1 日低）不足确认天数 -> 不触发"""
    seq = [("d0", 0.5), ("d1", 0.01), ("d2", 0.5), ("d3", 0.5)]
    m = _pool_gate_map(seq, enter_th=0.15)
    assert all(v is False for v in m.values())


# ---------------- 集成行为 ----------------

def test_pool_gate_blocks_reopen_in_bear(tmp_path):
    """初始池上涨后全池转跌（健康度<15% 触发 gate）-> 下跌段反弹修复票的
    新开仓被抑制；gate off 时同段正常开仓（对照组）。
    用静态池（非 universe_auto）隔离验证 gate 本身——universe_auto 的
    「空仓5日重选→新池 gate 重置」是另一条已设计路径。"""
    plans = {c: [(0, 200, 0.003), (200, N_DAYS, -0.006)]
             for c in ("600000", "600036", "000001", "000002",
                       "600037", "000003", "600040")}
    # 反弹票：下跌段中段反弹（金叉+动量转正 -> gate off 会重新开仓）
    plans["300001"] = [(0, 200, 0.003), (200, 258, -0.006),
                       (258, 300, 0.004), (300, N_DAYS, -0.006)]
    dates = _write_market(tmp_path, plans, seed0=70)
    start, end = dates[200], dates[N_DAYS - 1]
    universe = list(plans)
    # 7 只 DOWN 票动量转负后健康度 = 1/8 = 12.5% < 15% -> gate 触发且在
    # 反弹段保持（反弹票 1 只正，健康度仍 12.5%）
    base = {"name": "gate-test", "strategy_id": "momentum_slot", "params": {},
            "risk_config": {}, "period": "daily", "initial_capital": 1_000_000,
            "exclude_st": True, "universe": universe,
            "start_date": start, "end_date": end}
    rep_on = run_backtest(dict(base, pool_gate=True, pool_gate_enter_th=0.15),
                          data_dir=str(tmp_path))
    rep_off = run_backtest(dict(base), data_dir=str(tmp_path))

    opens_on = [t for t in rep_on["trade_log"] if t["type"] == "开仓"]
    opens_off = [t for t in rep_off["trade_log"] if t["type"] == "开仓"]
    assert len(opens_on) <= len(opens_off), "gate on 的开仓不应多于 gate off"
    # 对照组（gate off）在反弹段确实重新开仓 -> 场景有效
    assert any(t["time"][:10] >= dates[265] for t in opens_off), \
        "对照组应在反弹段重新开仓（否则场景构造无效）"
    # gate on：反弹段（健康度仍 <15%）无任何新开仓
    assert all(t["time"][:10] < dates[265] for t in opens_on), \
        "gate on 在健康度低于阈值的反弹段不应开仓"
    # 退出/止损管理照常
    assert any(t["side"] == "sell" for t in rep_on["trade_log"])


def test_pool_gate_off_is_backward_compatible(tmp_path):
    """pool_gate=False（显式）与缺省行为完全一致（向后兼容）"""
    dates = _write_market(tmp_path, {
        "600000": [(0, N_DAYS, 0.003)],
        "600036": [(0, N_DAYS, 0.003)],
    }, seed0=70)
    cfg_a = _auto_cfg(dates[200], dates[N_DAYS - 1], auto_top_x=1)
    cfg_b = _auto_cfg(dates[200], dates[N_DAYS - 1], auto_top_x=1, pool_gate=False)
    rep_a = run_backtest(cfg_a, data_dir=str(tmp_path))
    rep_b = run_backtest(cfg_b, data_dir=str(tmp_path))
    assert rep_a["metrics"]["total_return"] == rep_b["metrics"]["total_return"]
    assert rep_a["trade_log"] == rep_b["trade_log"]


# ---------------- validate ----------------

def test_validate_pool_gate_rejects_non_momentum():
    cfg = {"name": "v", "strategy_id": "ma_cross", "params": {},
           "universe": ["600000"], "pool_gate": True,
           "start_date": "2025-01-01", "end_date": "2025-06-01", "period": "daily"}
    try:
        validate_backtest_config(cfg)
        assert False, "ma_cross 开启 pool_gate 应被拦截"
    except HTTPException as e:
        assert "池级趋势开关" in (e.detail or "")


def test_validate_pool_gate_threshold_range():
    base = {"name": "v", "strategy_id": "momentum_t", "params": {},
            "universe": ["600000"], "start_date": "2025-01-01",
            "end_date": "2025-06-01", "period": "daily"}
    bad = dict(base, pool_gate=True, pool_gate_enter_th=0.8)
    try:
        validate_backtest_config(bad)
        assert False, "阈值越界应被拦截"
    except HTTPException as e:
        assert "pool_gate_enter_th" in (e.detail or "")
    ok = validate_backtest_config(dict(base, pool_gate=True, pool_gate_enter_th=0.15))
    assert ok["pool_gate"] is True
