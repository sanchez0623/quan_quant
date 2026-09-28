# -*- coding: utf-8 -*-
"""dragon_dip 分钟级模拟器（dragon_dip_minute）单测：合成日线+分钟线，
验证打板两种成交语义 / 分时低吸 / T+1 / 金字塔减仓 / 一字板拦截。"""
import polars as pl
import pytest

from app.engine import dragon_dip_minute as ddm


DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
        "2024-01-08", "2024-01-09", "2024-01-10"]
NEXT = {DAYS[i]: DAYS[i + 1] for i in range(len(DAYS) - 1)}


def _bar(day, hm, o, h, l, c, vol=1_000_000):
    return {"code": "000001", "date": f"{day} {hm}", "open": o, "high": h,
            "low": l, "close": c, "volume": vol, "amount": o * vol}


def _minute_df(bars):
    return pl.DataFrame(bars)


def make_ctx(cand_day, kinds, closes, limit_prices, prev_closes):
    """合成 DailyContext：单票 000001，主板 10%。closes/limit_prices/prev_closes
    为 day -> 值 的字典（日线口径，adj_factor 恒 1）。"""
    rows = []
    for d in DAYS:
        rows.append({
            "code": "000001", "date": d,
            "prev_close": prev_closes.get(d), "limit_price": limit_prices.get(d),
            "is_limit_close": False, "touched_limit": d == cand_day,
            "one_word": False, "consec_prev": 0, "prev_amount": 5e8,
            "close": closes.get(d), "close_hfq": closes.get(d),
            "ma5_hfq": closes.get(d, 10.0), "volume": 1e7,
            "vol_ma20": 1e7, "adj_factor": 1.0,
        })
    frame = pl.DataFrame(rows)
    gate = {d: (False, False) for d in DAYS}
    candidates = {cand_day: [("000001", frozenset(kinds))]}
    return ddm.DailyContext(days=DAYS, gate=gate, frame=frame,
                            candidates=candidates, yin_confirms={},
                            code_day={}, next_day=NEXT)


@pytest.fixture()
def patched_store(monkeypatch):
    basic = pl.DataFrame({"code": ["000001"], "name": ["合成票"], "st": [False],
                          "delisted": [False]})
    monkeypatch.setattr(ddm.store, "read_stock_basic", lambda *a, **k: basic)
    return basic


def run(cfg, ctx, minute_bars, monkeypatch):
    monkeypatch.setattr(ddm, "build_daily_context", lambda codes, p: ctx)
    monkeypatch.setattr(ddm, "load_minute5",
                        lambda codes, s, e, d: {"000001": _minute_df(minute_bars)})
    return ddm.run_minute_backtest(cfg)


BASE_CFG = {
    "name": "合成测试", "start_date": "2024-01-02", "end_date": "2024-01-10",
    "initial_capital": 1_000_000.0,
    "params": {"entry_type": "all", "dban_fill": "break"},
    "risk_config": {"stop_loss_pct": 5.0, "max_holdings": 2,
                    "max_position_pct_per_stock": 60.0,
                    "max_drawdown_breaker": 30.0},
}


def test_dban_break_fill_and_stop(patched_store, monkeypatch):
    """炸板 bar 成交（break 语义）-> 次日跌幅触发止损 -> T+1 卖出。"""
    closes = {d: 14.10 if d == "2024-01-05" else 10.0 for d in DAYS}
    limits = {d: (14.64 if d == "2024-01-05" else 11.0) for d in DAYS}
    prevs = {d: (13.31 if d == "2024-01-05" else 10.0) for d in DAYS}
    ctx = make_ctx("2024-01-05", {"dban", "dip"}, closes, limits, prevs)
    bars = [
        # 01-05 09:35 触板 14.64 未封收 14.10 -> dban(break) 成交
        _bar("2024-01-05", "09:35", 13.40, 14.64, 13.40, 14.10),
        _bar("2024-01-05", "09:40", 14.10, 14.20, 14.00, 14.15),
        # 01-08 收盘跌破止损线 14.65*0.95=13.92 -> 挂止损
        _bar("2024-01-08", "09:35", 13.95, 14.00, 13.80, 13.85),
        _bar("2024-01-08", "09:40", 13.80, 13.90, 13.70, 13.75),
    ]
    rep = run(dict(BASE_CFG), ctx, bars, monkeypatch)
    buys = [t for t in rep["trade_log"] if t["side"] == "buy"]
    sells = [t for t in rep["trade_log"] if t["side"] == "sell"]
    assert len(buys) == 1 and buys[0]["time"] == "2024-01-05 09:35"
    assert buys[0]["reason"].startswith("打板")
    assert abs(buys[0]["price"] - 14.64 * 1.001) < 0.001  # 涨停价+滑点
    assert len(sells) == 1 and sells[0]["type"] == "止损"
    assert sells[0]["time"] == "2024-01-08 09:40"          # T+1 且次根开盘
    assert sells[0]["pnl"] < 0


def test_dban_touch_fill_on_seal(patched_store, monkeypatch):
    """touch 语义：封板 bar 也按涨停价成交。"""
    closes = {d: (14.64 if d == "2024-01-05" else 10.0) for d in DAYS}
    limits = {d: (14.64 if d == "2024-01-05" else 11.0) for d in DAYS}
    prevs = {d: (13.31 if d == "2024-01-05" else 10.0) for d in DAYS}
    ctx = make_ctx("2024-01-05", {"dban", "dip"}, closes, limits, prevs)
    bars = [
        _bar("2024-01-05", "09:35", 14.20, 14.64, 14.10, 14.64),  # 触板封死
        _bar("2024-01-05", "09:40", 14.64, 14.64, 14.60, 14.64),
    ]
    cfg = dict(BASE_CFG)
    cfg["params"] = {"entry_type": "all", "dban_fill": "touch"}
    rep = run(cfg, ctx, bars, monkeypatch)
    buys = [t for t in rep["trade_log"] if t["side"] == "buy"]
    assert len(buys) == 1 and abs(buys[0]["price"] - 14.64 * 1.001) < 0.001


def test_one_word_bar_no_fill(patched_store, monkeypatch):
    """一字板 bar（o=h=l=c=涨停价）不成交。"""
    closes = {d: (14.64 if d == "2024-01-05" else 10.0) for d in DAYS}
    limits = {d: (14.64 if d == "2024-01-05" else 11.0) for d in DAYS}
    prevs = {d: (13.31 if d == "2024-01-05" else 10.0) for d in DAYS}
    ctx = make_ctx("2024-01-05", {"dban", "dip"}, closes, limits, prevs)
    bars = [_bar("2024-01-05", "09:35", 14.64, 14.64, 14.64, 14.64)]
    rep = run(dict(BASE_CFG), ctx, bars, monkeypatch)
    assert not [t for t in rep["trade_log"] if t["side"] == "buy"]


def test_dip_pullback_entry(patched_store, monkeypatch):
    """分时低吸：触板后自日内高点回落 3~7% -> 次根开盘买。"""
    closes = {d: (14.15 if d == "2024-01-05" else 10.0) for d in DAYS}
    limits = {d: (14.64 if d == "2024-01-05" else 11.0) for d in DAYS}
    prevs = {d: (13.31 if d == "2024-01-05" else 10.0) for d in DAYS}
    ctx = make_ctx("2024-01-05", {"dip"}, closes, limits, prevs)
    bars = [
        _bar("2024-01-05", "09:35", 14.30, 14.64, 14.20, 14.55),  # 触板未破回落区
        _bar("2024-01-05", "09:40", 14.40, 14.45, 14.10, 14.15),  # 回落 3.3% 触发
        _bar("2024-01-05", "09:45", 14.15, 14.20, 14.05, 14.10),  # 次根开盘成交
    ]
    cfg = dict(BASE_CFG)
    cfg["params"] = {"entry_type": "dip"}
    rep = run(cfg, ctx, bars, monkeypatch)
    buys = [t for t in rep["trade_log"] if t["side"] == "buy"]
    assert len(buys) == 1 and buys[0]["time"] == "2024-01-05 09:45"
    assert buys[0]["reason"].startswith("分时低吸")
    assert abs(buys[0]["price"] - 14.15 * 1.001) < 0.001


def test_pyr_reduce_on_seal(patched_store, monkeypatch):
    """持仓期封板 -> 金字塔减仓 30%（次根开盘卖）。"""
    closes = {"2024-01-02": 10.5, "2024-01-03": 10.7, "2024-01-04": 10.7,
              "2024-01-05": 10.7, "2024-01-08": 15.51, "2024-01-09": 15.51,
              "2024-01-10": 15.51}
    limits = {"2024-01-02": 11.0, "2024-01-03": 11.0, "2024-01-04": 11.0,
              "2024-01-05": 11.0, "2024-01-08": 15.51, "2024-01-09": 17.06,
              "2024-01-10": 17.06}
    prevs = {"2024-01-02": 10.0, "2024-01-03": 10.0, "2024-01-04": 10.0,
             "2024-01-05": 10.0, "2024-01-08": 14.10, "2024-01-09": 15.51,
             "2024-01-10": 15.51}
    # 01-02 触板候选（bar 触板 11.0 炸板收 10.5）买入；01-08 封板 15.51 减仓
    ctx = make_ctx("2024-01-02", {"dban", "dip"}, closes, limits, prevs)
    bars = [
        _bar("2024-01-02", "09:35", 10.2, 11.0, 10.1, 10.5),
        _bar("2024-01-02", "09:40", 10.5, 10.6, 10.4, 10.55),
        # 01-03~05 横盘（ma5=close 不破位；close>10.46 不止损；<limit 不减仓）
        _bar("2024-01-03", "09:35", 10.65, 10.75, 10.60, 10.70),
        _bar("2024-01-04", "09:35", 10.70, 10.80, 10.65, 10.75),
        _bar("2024-01-05", "09:35", 10.75, 10.85, 10.70, 10.80),
        # 01-08 封板 15.51（limit=14.10*1.1=15.51）-> 金字塔减仓
        _bar("2024-01-08", "09:35", 15.00, 15.51, 14.90, 15.51),
        _bar("2024-01-08", "09:40", 15.51, 15.55, 15.40, 15.50),
    ]
    rep = run(dict(BASE_CFG), ctx, bars, monkeypatch)
    reduces = [t for t in rep["trade_log"] if t["type"] == "减仓"]
    assert len(reduces) == 1 and reduces[0]["time"] == "2024-01-08 09:40"
    assert reduces[0]["reason"].startswith("涨停晋级金字塔减仓")
    buy = next(t for t in rep["trade_log"] if t["side"] == "buy")
    assert reduces[0]["volume"] == int(buy["volume"] * 0.3 // 100) * 100


def test_t1_same_day_sell_blocked(patched_store, monkeypatch):
    """T+1：当日买入当日触止损 -> 次日才能卖。"""
    closes = {d: (14.10 if d == "2024-01-05" else 10.0) for d in DAYS}
    limits = {d: (14.64 if d == "2024-01-05" else 11.0) for d in DAYS}
    prevs = {d: (13.31 if d == "2024-01-05" else 10.0) for d in DAYS}
    ctx = make_ctx("2024-01-05", {"dban", "dip"}, closes, limits, prevs)
    bars = [
        _bar("2024-01-05", "09:35", 13.40, 14.64, 13.40, 14.10),  # 买入
        _bar("2024-01-05", "09:40", 14.10, 14.15, 13.00, 13.10),  # 当日深跌触发止损
        _bar("2024-01-05", "09:45", 13.10, 13.15, 13.00, 13.05),  # 次根：T+1 拦截
    ]
    rep = run(dict(BASE_CFG), ctx, bars, monkeypatch)
    sells = [t for t in rep["trade_log"] if t["side"] == "sell"]
    assert not sells  # 当日买入不可卖，且 01-05 后无 bars -> 挂单自然消失


# ---------------- 策略注册元数据 + runner 分流（放出到回测系统） ----------------

def test_minute_strategy_registered():
    """注册进 REGISTRY：periods 仅 minute5；param_schema 覆盖引擎全部默认参数"""
    from app.engine.strategies import REGISTRY, validate_params
    s = REGISTRY.get("dragon_dip_minute")
    assert s is not None and s.periods == ["minute5"]
    schema_keys = {p["key"] for p in s.param_schema}
    assert set(ddm.DEFAULT_PARAMS) <= schema_keys, "param_schema 必须覆盖引擎参数"
    ok, _ = validate_params("dragon_dip_minute", {"entry_type": "dip"})
    assert ok
    bad, _ = validate_params("dragon_dip_minute", {"entry_type": "nope"})
    assert not bad


def test_runner_dispatch_to_minute_engine(monkeypatch):
    """run_backtest 按 strategy_id 分流到 run_minute_backtest（不走 bar-by-bar）"""
    from app.engine import runner
    sentinel = {"engine_version": "dragon_dip_minute_v1"}
    called = {}
    monkeypatch.setattr(
        "app.engine.dragon_dip_minute.run_minute_backtest",
        lambda cfg: called.update(cfg=cfg) or sentinel)
    rep = runner.run_backtest({"strategy_id": "dragon_dip_minute",
                               "params": {"top_n": 1}})
    assert rep is sentinel
    assert called["cfg"]["strategy_id"] == "dragon_dip_minute"
