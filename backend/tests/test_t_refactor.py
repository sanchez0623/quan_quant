# -*- coding: utf-8 -*-
import math
import polars as pl
from app.data import store, synthetic
from app.engine.runner import run_backtest


def _write_series(tmp_path, day_prices):
    "构造确定性分钟序列：每日从昨收线性走向 day_prices[di]，合成日内48根"
    codes = ["600000"]
    rows = []
    dates = synthetic.trade_dates(len(day_prices))
    prev = day_prices[0]
    for di, d in enumerate(dates):
        target = day_prices[di]
        for k, hhmm in enumerate(synthetic.BAR_TIMES):
            frac = (k + 1) / len(synthetic.BAR_TIMES)
            c = prev + (target - prev) * frac
            o = prev + (target - prev) * (k / len(synthetic.BAR_TIMES))
            rows.append({"code": codes[0], "date": f"{d} {hhmm}",
                         "open": round(o, 3), "high": round(max(o, c) * 1.001, 3),
                         "low": round(min(o, c) * 0.999, 3), "close": round(c, 3),
                         "volume": 100_000, "amount": round(c * 100_000, 2)})
            prev = c
    mdf = pl.DataFrame(rows)
    ddf = (mdf.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
           .group_by("day").agg(
               pl.col("open").first().alias("open"),
               pl.col("high").max().alias("high"),
               pl.col("low").min().alias("low"),
               pl.col("close").last().alias("close"),
               pl.col("volume").sum().alias("volume"),
               pl.col("amount").sum().alias("amount"),
               pl.lit(codes[0]).alias("code"))
           .with_columns(pl.col("day").alias("date")).drop("day")
           .select(["code", "date", "open", "high", "low", "close", "volume", "amount"]))
    store.write_minute5(codes[0], mdf, str(tmp_path))
    store.write_daily(ddf, str(tmp_path))
    dates_all = sorted(ddf["date"].to_list())
    store.write_calendar(pl.DataFrame({"date": dates_all, "is_open": [1] * len(dates_all)}),
                         str(tmp_path))
    store.write_adj_factor(pl.DataFrame({"code": [codes[0]] * len(rows), "date": mdf["date"].to_list(),
        "adj_factor": [1.0] * len(rows)}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({"code": codes, "name": ["测试股600000"], "st": [False], "list_date": ["20100101"]}),
        str(tmp_path))
    return dates_all[0], dates_all[-1]


def _grid_cfg(start, end, t_mode="grid", **over):
    cfg = {"name": "t-refactor", "strategy_id": "grid_t",
        "params": {"base_pct": 30, "grid_atr_mult": 0.5, "atr_period": 2,
                     "max_t_times": 6, "max_adds": 0,
                     "t_mode": t_mode, "t_debt_max_days": 1},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 60,
                        "max_total_position_pct": 100, "max_intraday_trades": 12},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "minute5", "initial_capital": 1_000_000,
    }
    cfg["params"].update(over)
    return cfg

def test_t_mode_time_signals():
    "D 时点规律T：09:35 高抛 / 14:50 买回（策略层信号）"
    from app.engine.strategies.momentum_t import MomentumTStrategy
    strat = MomentumTStrategy()
    p = {k["key"]: k["default"] for k in strat.param_schema}
    p["t_mode"] = "time"
    df = pl.DataFrame({"date": ["2026-01-05 09:30", "2026-01-05 09:35", "2026-01-05 14:50"],
        "close": [10.0, 10.0, 10.0],
        "atr_pct": [0.02, 0.02, 0.02], "bias": [0.0, 0.0, 0.0],
        "vol_pos": [0.5, 0.5, 0.5], "breakout": [False, False, False],
        "dif": [1.0, 1.0, 1.0], "dea": [0.0, 0.0, 0.0],
        "ma_slow": [9.0, 9.0, 9.0], "slope": [1.0, 1.0, 1.0],
        "day_idx": [0, 1, 2],
    })
    out = {s.name: s.to_list() for s in strat._walk(df, p, {df["date"][0][:10]}, None)}
    sells = [i for i in range(len(out["signal"])) if out["signal"][i] == -1 and out["tag"][i] == "做T"]
    buys = [i for i in range(len(out["signal"])) if out["signal"][i] == 1 and out["tag"][i] == "做T"]
    assert sells == [1], "09:35 应触发时点T高抛（signal=-1）: %s" % sells
    assert buys == [2], "14:50 应触发时点T买回（signal=1）: %s" % buys


def test_t_mode_off_no_t_trades(tmp_path):
    "C 关闭做T：t_mode=off 全程无做T交易"
    start, end = _write_series(tmp_path, [10.0, 10.3, 10.3, 10.3])
    cfg = _grid_cfg(start, end, t_mode="off")
    report = run_backtest(cfg, data_dir=str(tmp_path))
    assert not any(t["type"] == "做T" for t in report["trade_log"]), "off 模式不应有做T交易"


def test_t_paired_metrics_and_open_debts(tmp_path):
    "L1/L2 报表诚实化：配对口径三指标 + 期末未闭环债务 + engine_version 标记"
    start, end = _write_series(tmp_path, [10.0, 10.3, 10.15, 10.3])
    cfg = _grid_cfg(start, end)
    report = run_backtest(cfg, data_dir=str(tmp_path))
    m = report["metrics"]
    assert report.get("engine_version") == "t_refactor_v1"
    assert "t_pnl_closed" in m
    assert "t_payoff" in m
    assert isinstance(report.get("t_open_debts"), list)
    assert isinstance(report.get("t_reject_events"), list)
    t_trades = [t for t in report["trade_log"] if t["type"] == "做T"]
    for t in t_trades:
        assert t.get("t_mode") is not None, "做T记录应带 t_mode 标签"
def test_t_debt_timeout_reclassifies(tmp_path):
    start, end = _write_series(tmp_path, [10.0, 10.3, 10.3, 10.3])
    cfg = _grid_cfg(start, end)
    report = run_backtest(cfg, data_dir=str(tmp_path))
    exp = [x for x in report["trade_log"] if "T债务超时转减仓" in (x.get("reason") or "")]
    assert exp
    assert all(x["tag"] == "减仓" for x in exp)

def test_t_discipline_rejects_chase(tmp_path):
    start, end = _write_series(tmp_path, [10.0, 10.3, 10.15, 10.3])
    cfg = _grid_cfg(start, end, t_mode="discipline", reentry_discount=2.0)
    report = run_backtest(cfg, data_dir=str(tmp_path))
    events = report.get("t_reject_events") or []
    assert events
    for e in events:
        assert e["type"] == "discipline"
        assert e["buy_price"] > e["sell_px_avg"] * 0.98

def test_t_chase_limit_grid(tmp_path):
    start, end = _write_series(tmp_path, [10.0, 10.3, 10.15, 10.3])
    cfg = _grid_cfg(start, end, t_max_chase_pct=0.5)
    report = run_backtest(cfg, data_dir=str(tmp_path))
    events = report.get("t_reject_events") or []
    for e in events:
        if e["type"] == "chase":
            assert e["buy_price"] > e["sell_px_avg"] * 1.005
    assert "t_pnl" in report["metrics"]