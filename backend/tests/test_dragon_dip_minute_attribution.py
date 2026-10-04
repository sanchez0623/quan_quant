# -*- coding: utf-8 -*-
"""dragon_dip_minute 收益归因字段（1a + 2b）回归测试。

1a：接入 stats.build_metrics，但暂不接做T配对 -> t_pnl 恒为 0；stop_loss_pnl 可用。
2b：只「增补」归因字段，原有 12 个指标保留分钟版口径 -> 零数值漂移。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.dragon_dip_minute import _report  # noqa: E402

INITIAL = 1_000_000.0
FINAL = 999_500.0


def _trade(seq, side, day, ttype, pnl=None, fee=0.0, gid=1, open_time="2024-01-02"):
    return {
        "trade_id": seq, "code": "600000", "name": "测试", "time": day, "side": side,
        "price": 10.0, "hfq_price": 10.0, "volume": 100, "amount": 1000.0,
        "fee": fee, "type": ttype, "group_id": gid, "reason": ttype,
        "pnl": pnl, "tag": "开仓", "open_time": open_time, "t_mode": None,
    }


def _fixture():
    trades = [
        _trade(1, "buy", "2024-01-02", "开仓", fee=5.0),
        _trade(2, "buy", "2024-01-03", "做T买回", fee=3.0, open_time="2024-01-03"),
        _trade(3, "sell", "2024-01-10", "止损", pnl=-1000.0, fee=6.0, gid=1),
        _trade(4, "sell", "2024-01-11", "减仓", pnl=300.0, fee=4.0, gid=2),
        _trade(5, "sell", "2024-01-12", "做T", pnl=200.0, fee=4.0, gid=3),
        # 同一 group 两笔：分钟版按「整组合计」判定胜负，build_metrics 按「逐笔」判定，
        # 用它证明 merge 没有覆盖分钟版口径。
        _trade(6, "sell", "2024-01-15", "减仓", pnl=-100.0, fee=2.0, gid=4),
        _trade(7, "sell", "2024-01-16", "减仓", pnl=50.0, fee=2.0, gid=4),
    ]
    equity_curve = [
        {"date": "2024-01-02", "equity": INITIAL, "adjusted_equity": INITIAL},
        {"date": "2024-01-31", "equity": FINAL, "adjusted_equity": FINAL},
    ]
    w_state = {"total": 0.0, "nav_profit": 0.0, "nav_times": 0, "nav_base": 0.0, "log": []}
    return trades, equity_curve, w_state


def _metrics():
    trades, equity_curve, w_state = _fixture()
    rep = _report({"name": "归因测试"}, {}, [], trades, equity_curve, {}, INITIAL, w_state)
    return rep["metrics"]


def test_attribution_fields_are_added():
    m = _metrics()
    assert m["stop_loss_pnl"] == -1000.0
    assert m["reduce_pnl"] == 250.0          # 300 - 100 + 50
    assert m["open_pnl"] == 200.0            # 做T卖出的盈亏落在这里（配对未接入）
    assert m["commission_total"] == 26.0     # 5+3+6+4+4+2+2
    assert m["adj_pnl"] == -500.0            # 已是调整净值口径，不重复计入提取
    assert m["start_equity"] == INITIAL
    assert m["end_equity"] == FINAL


def test_t_pnl_is_zero_and_flagged_unpaired():
    """1a 的显式代价：t_pnl 恒为 0，报告里必须带 note，避免被当成真值读。"""
    m = _metrics()
    assert m["t_pnl"] == 0.0
    assert m["t_pnl_closed"] is None
    assert "t_pnl" in m["attribution_note"]


def test_legacy_metrics_keep_minute_engine_definition():
    """2b：收益/风险/胜率类字段必须仍是分钟版口径（零数值漂移）。"""
    m = _metrics()
    assert m["total_trades"] == 7
    assert m["total_return"] == round(FINAL / INITIAL - 1, 6)
    assert m["total_pnl"] == -550.0          # 全部卖出的 pnl 合计
    # 分钟版：按 group 合计判胜负 -> {1:-1000, 2:300, 3:200, 4:-50} = 2/4
    # build_metrics 逐笔口径会算出 3/5，若被覆盖这里会失败
    assert m["win_rate"] == 0.5