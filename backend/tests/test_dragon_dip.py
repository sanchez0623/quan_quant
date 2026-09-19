# -*- coding: utf-8 -*-
"""龙头低吸（dragon_dip）策略 + limitup_core 涨停基础设施测试。

场景全部手造日线（adj_factor=1，主板 10% 板），聚焦：
- 涨停价精确性（四舍五入到分/板块差异化）
- 连板/炸板/一字判定与市场情绪聚合
- 三类买点（炸板/首阴/竞价）entry_type 切换
- 金字塔减仓（晋级减仓×pyr_max）、破5日线清仓
- 连板高度排名 top_n、情绪门控停开仓
"""
import polars as pl

from app.engine import limitup_core as lc
from app.engine.strategies.dragon_dip import DragonDipStrategy


# ---------------- 手造数据工具 ----------------

def _bar(d, o, h, l, c, vol=1e7, amt=5e8):
    return {"date": d, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "amount": amt, "adj_factor": 1.0, "code": "600001"}


D = [f"2025-01-{x:02d}" for x in (1, 2, 3, 6, 7, 8, 9, 10, 13, 14)]


def _df(bars, code="600001"):
    df = pl.DataFrame(bars)
    return df.with_columns(pl.lit(code).alias("code"))


def _params(**over):
    p = {k["key"]: k["default"] for k in DragonDipStrategy().param_schema}
    p.update({"regime_gate_on": "off", "new_stock_days": 0})
    p.update(over)
    return p


def _signals(out, code="600001"):
    rows = out[code].to_dicts()
    return [(r["date"], r["signal"], r["tag"], r["reason"], r["budget_pct"],
             r["reduce_pct"]) for r in rows if r["signal"] != 0]


# ---------------- limitup_core ----------------

def test_limit_price_rounding_and_boards():
    assert lc.limit_price(10.13, "600001") == 11.14      # 10% 四舍五入到分
    assert lc.limit_price(10.00, "600001") == 11.00
    assert lc.limit_price(10.00, "300001") == 12.00      # 创业板 20%
    assert lc.limit_price(10.00, "688001") == 12.00      # 科创板 20%
    assert lc.limit_price(10.00, "600001", is_st=True) == 10.50  # ST 5%
    assert lc.limit_price(0, "600001") is None


def test_daily_flags_consec_broken_one_word():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),              # 基期
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),             # 涨停 consec1
        _bar(D[2], 11.50, 12.10, 11.40, 12.10),             # 涨停 consec2
        _bar(D[3], 12.90, 13.31, 12.60, 12.80),             # 触板未封 -> 炸板
        _bar(D[4], 13.10, 14.08, 13.10, 14.08),             # 涨停 consec1
        _bar(D[5], 15.49, 15.49, 15.49, 15.49),             # 一字板（前收14.08x1.1）
    ]
    f = lc.daily_flags(_df(bars), "600001").to_dicts()
    assert [r["is_limit_close"] for r in f] == [False, True, True, False, True, True]
    assert [r["touched_limit"] for r in f] == [False, True, True, True, True, True]
    assert [r["limit_broken"] for r in f] == [False, False, False, True, False, False]
    assert [r["consec_boards"] for r in f] == [0, 1, 2, 0, 1, 2]
    assert [r["one_word"] for r in f] == [False, False, False, False, False, True]
    assert f[3]["limit_price"] == 13.31


def test_market_sentiment_and_regime():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),
        _bar(D[2], 11.50, 12.10, 11.40, 12.10),
        _bar(D[3], 12.90, 13.31, 12.60, 12.80),
    ]
    sent = lc.market_sentiment([lc.daily_flags(_df(bars), "600001")]).to_dicts()
    assert [r["n_limit"] for r in sent] == [0, 1, 1, 0]
    assert [r["n_touched"] for r in sent] == [0, 1, 1, 1]
    assert sent[3]["broken_rate"] == 1.0
    assert [r["max_boards"] for r in sent] == [0, 1, 2, 0]
    assert sent[2]["promote_rate"] == 1.0 and sent[3]["promote_rate"] == 0.0
    assert abs(sent[3]["limit_premium"] - (12.80 / 12.10 - 1)) < 1e-9
    reg = lc.regime_gate(pl.DataFrame(sent), broken_rate_th=0.5,
                         n_limit_floor=0, regime_ma_n=2,
                         euphoria_boards=3).to_dicts()
    assert reg[3]["gate_off"] is True      # 炸板率 1.0 且最高板回落
    assert all(not r["gate_off"] for r in reg[:3])
    assert all(not r["euphoria"] for r in reg)


# ---------------- 买点：炸板 ----------------

def _zha_bars():
    return [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.00, 10.15, 9.95, 10.00),    # 基期第2天（喂饱 vol_ma20 窗口）
        _bar(D[2], 10.30, 11.00, 10.25, 11.00),
        _bar(D[3], 11.50, 12.10, 11.40, 12.10),
        _bar(D[4], 12.90, 13.31, 12.60, 12.80),   # 炸板：回落 3.83%
    ]


def test_zha_entry():
    out = DragonDipStrategy().prepare({"600001": _df(_zha_bars())}, _params())
    sig = _signals(out)
    assert len(sig) == 1 and sig[0][0] == D[4]
    assert sig[0][2] == "开仓" and "炸板低吸" in sig[0][3]
    assert sig[0][4] == 50.0


def test_entry_type_switch_suppresses_zha():
    out = DragonDipStrategy().prepare({"600001": _df(_zha_bars())},
                                      _params(entry_type="yin"))
    assert _signals(out) == []


def test_new_stock_guard_suppresses_entry():
    out = DragonDipStrategy().prepare({"600001": _df(_zha_bars())},
                                      _params(new_stock_days=6))
    assert _signals(out) == []


# ---------------- 买点：首阴 ----------------

def test_yin_entry():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),
        _bar(D[2], 11.50, 12.10, 11.40, 12.10),
        _bar(D[3], 12.30, 13.31, 12.20, 13.31),   # 三连板
        _bar(D[4], 13.60, 13.70, 12.70, 12.77, vol=1.5e7),  # 首阴 -4.06%，未触板
    ]
    out = DragonDipStrategy().prepare({"600001": _df(bars)}, _params())
    sig = _signals(out)
    assert len(sig) == 1 and sig[0][0] == D[4] and "首阴低吸" in sig[0][3]


def test_yin_rejects_ma_break():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),
        _bar(D[2], 11.50, 12.10, 11.40, 12.10),
        _bar(D[3], 12.30, 13.31, 12.20, 13.31),
        _bar(D[4], 13.60, 13.70, 9.00, 9.10, vol=1.5e7),   # 深跌破5日线 -> 不算首阴低吸
    ]
    out = DragonDipStrategy().prepare({"600001": _df(bars)}, _params())
    sig = _signals(out)
    assert not [s for s in sig if "首阴低吸" in s[3]]


# ---------------- 买点：竞价低开 ----------------

def test_gap_entry():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),   # 涨停
        _bar(D[2], 10.40, 11.50, 10.35, 11.20),   # 低开 5.45% 收复开盘
    ]
    out = DragonDipStrategy().prepare({"600001": _df(bars)}, _params())
    sig = _signals(out)
    assert len(sig) == 1 and sig[0][0] == D[2] and "竞价低吸" in sig[0][3]


def test_gap_requires_recovery():
    bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.30, 11.00, 10.25, 11.00),
        _bar(D[2], 10.40, 10.80, 10.10, 10.20),   # 低开后收不上开盘 -> 无承接
    ]
    out = DragonDipStrategy().prepare({"600001": _df(bars)}, _params())
    assert _signals(out) == []


# ---------------- 卖出：金字塔减仓 / 走弱清仓 ----------------

def _holding_bars():
    """炸板开仓后连续 4 个涨停，再深跌清仓"""
    return [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.00, 10.15, 9.95, 10.00),
        _bar(D[2], 10.30, 11.00, 10.25, 11.00),
        _bar(D[3], 11.50, 12.10, 11.40, 12.10),
        _bar(D[4], 12.90, 13.31, 12.60, 12.80),   # 开仓（炸板低吸）
        _bar(D[5], 12.90, 14.08, 12.85, 14.08),   # 晋级 -> 减仓1
        _bar(D[6], 14.30, 15.49, 14.50, 15.49),   # 晋级 -> 减仓2
        _bar(D[7], 15.60, 17.04, 15.55, 17.04),   # 晋级 -> 减仓3
        _bar(D[8], 17.20, 18.74, 17.10, 18.74),   # 晋级 -> 达 pyr_max 不再减
        _bar(D[9], 16.00, 16.20, 14.90, 15.00),   # 跌破5日线 -> 清仓
    ]


def test_pyramid_reduce_and_ma_break_clear():
    out = DragonDipStrategy().prepare({"600001": _df(_holding_bars())}, _params())
    sig = _signals(out)
    entries = [s for s in sig if s[2] == "开仓"]
    reduces = [s for s in sig if s[2] == "减仓"]
    clears = [s for s in sig if s[1] == -1 and s[2] == ""]
    assert len(entries) == 1 and entries[0][0] == D[4]
    assert [r[0] for r in reduces] == [D[5], D[6], D[7]]
    assert all(r[5] == 30.0 for r in reduces)
    assert len(clears) == 1 and clears[0][0] == D[9] and "5日线" in clears[0][3]


# ---------------- 排名与门控 ----------------

def test_rank_top_n_prefers_higher_board():
    a = _df(_zha_bars(), code="600001")            # 开仓日 consec_prev=2
    b_bars = [
        _bar(D[0], 10.00, 10.20, 9.90, 10.00),
        _bar(D[1], 10.00, 10.15, 9.95, 10.00),
        _bar(D[2], 10.00, 10.20, 9.90, 10.00),
        _bar(D[3], 10.00, 10.20, 9.90, 10.00),
        _bar(D[4], 10.20, 11.00, 10.10, 10.50),    # 同日触板炸板，无连板
    ]
    b = _df(b_bars, code="000001")
    out = DragonDipStrategy().prepare({"600001": a, "000001": b},
                                      _params(top_n=1))
    opened = {c: [s for s in _signals(out, c) if s[2] == "开仓"]
              for c in ("600001", "000001")}
    assert len(opened["600001"]) == 1 and not opened["000001"]


def test_regime_gate_blocks_entry():
    out = DragonDipStrategy().prepare({"600001": _df(_zha_bars())},
                                      _params(regime_gate_on="on"))
    assert _signals(out) == []   # 单票池 n_limit<20 -> 冰点门控停开仓
