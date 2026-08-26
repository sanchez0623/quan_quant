# -*- coding: utf-8 -*-
"""引擎单元测试：直接函数调用 runner（不走进程池）"""
import math

import polars as pl
import pytest

from app.data import store, synthetic
from app.engine.broker import Broker
from app.engine.runner import run_backtest

METRIC_KEYS = [
    "total_return", "annual_return", "max_drawdown", "sharpe", "sortino", "calmar",
    "win_rate", "profit_loss_ratio", "total_trades", "total_pnl", "avg_hold_days",
    "t_trade_count", "t_win_rate", "t_pnl", "open_pnl", "add_pnl", "reduce_pnl",
    "stop_loss_pnl", "commission_total", "start_equity", "end_equity",
]


def make_config(demo_env, **over):
    data_dir, start, end = demo_env
    cfg = {
        "name": "engine-test", "strategy_id": "ma_cross",
        "params": {"fast": 5, "slow": 20, "max_adds": 2},
        "risk_config": {"max_position_pct_per_stock": 30,
                        "max_total_position_pct": 100,
                        "stop_loss_mode": "fixed", "stop_loss_pct": 8.0,
                        "atr_period": 14, "atr_multiplier": 2.0,
                        "take_profit_pct": 0, "trailing_stop_pct": 0,
                        "max_drawdown_breaker": 30, "max_intraday_trades": 4},
        "universe": ["600000", "000001", "600036"],
        "start_date": start, "end_date": end, "period": "daily",
        "initial_capital": 1_000_000, "slippage_pct": 0.001,
        "commission_rate": 0.0003, "commission_min": 5, "stamp_tax": 0.001,
        "transfer_fee": 0.00001, "exclude_st": True,
    }
    cfg.update(over)
    return cfg, data_dir


# ---------------- 1. 报告结构完整 ----------------

def test_ma_cross_daily_report_structure(demo_env):
    cfg, data_dir = make_config(demo_env)
    report = run_backtest(cfg, data_dir=data_dir)
    for key in ("metrics", "equity_curve", "monthly_returns", "trade_log",
                "position_snapshots", "config", "name"):
        assert key in report, f"缺少 report 字段: {key}"
    for k in METRIC_KEYS:
        assert k in report["metrics"], f"缺少 metrics 字段: {k}"
    assert report["equity_curve"], "equity_curve 为空"
    for p in report["equity_curve"]:
        assert set(p) >= {"date", "equity", "drawdown", "position_ratio"}
    assert report["position_snapshots"], "position_snapshots 为空"
    for s in report["position_snapshots"]:
        assert set(s) >= {"date", "cash", "market_value", "positions"}
    for t in report["trade_log"]:
        assert set(t) >= {"trade_id", "code", "name", "time", "side", "price",
                          "volume", "amount", "fee", "type", "group_id", "reason", "pnl"}
        assert t["side"] in ("buy", "sell")
        assert t["type"] in ("开仓", "加仓", "减仓", "做T", "止损", "止盈", "清仓")
    # 开仓 pnl 为空，平仓 pnl 有值
    for t in report["trade_log"]:
        if t["side"] == "buy":
            assert t["pnl"] is None
        else:
            assert isinstance(t["pnl"], (int, float))
    # 月度收益
    assert report["monthly_returns"], "monthly_returns 为空"
    for m in report["monthly_returns"]:
        assert set(m) >= {"year", "month", "return"}
    # 有成交（合成随机游走必然出现均线交叉）
    assert len(report["trade_log"]) > 0
    assert report["metrics"]["start_equity"] == pytest.approx(1_000_000)


# ---------------- 2. T+1：当日买入当日不可卖 ----------------

def test_t_plus_1_no_same_day_round_trip(demo_env):
    cfg, data_dir = make_config(demo_env)
    report = run_backtest(cfg, data_dir=data_dir)
    sells = [t for t in report["trade_log"] if t["side"] == "sell"]
    assert sells, "应有平仓记录"
    for t in sells:
        open_day = (t.get("open_time") or t["time"])[:10]
        assert open_day != t["time"][:10], \
            f"日线T+1违规：{t['code']} 当日 {t['time'][:10]} 买入并卖出"


def test_t_plus_1_sellable_date_blocks_sale(demo_env):
    """直接验证：sellable_date 为下一交易日时当日卖不出（通过止损不触达实现）"""
    cfg, data_dir = make_config(demo_env, risk_config={
        "max_position_pct_per_stock": 30, "max_total_position_pct": 100,
        "stop_loss_mode": "fixed", "stop_loss_pct": 0.1,  # 极紧止损：买入次日即触发
        "take_profit_pct": 0, "trailing_stop_pct": 0,
        "max_drawdown_breaker": 30, "max_intraday_trades": 4})
    report = run_backtest(cfg, data_dir=data_dir)
    buys = [t for t in report["trade_log"] if t["side"] == "buy"]
    sells = [t for t in report["trade_log"] if t["side"] == "sell"]
    # 所有卖出的持仓开仓日 != 卖出日
    for t in sells:
        assert (t.get("open_time") or "")[:10] != t["time"][:10]


# ---------------- 3. 手续费 ----------------

def test_fee_calculation():
    b = Broker(slippage_pct=0.001, commission_rate=0.0003, commission_min=5,
               stamp_tax=0.001, transfer_fee=0.00001)
    amount = 100_000
    # 双边费用：经手万0.341 + 证管万0.2 + 过户万0.1 = 万0.641 -> 6.41
    # 买单：佣金 max(30,5)=30 + 6.41 = 36.41
    assert b.buy_fee(amount) == pytest.approx(36.41)
    # 卖单：佣金 30 + 印花税 100 + 6.41 = 136.41
    assert b.sell_fee(amount) == pytest.approx(136.41)
    # 最低佣金：小单 amount=10000 -> 佣金 max(3,5)=5（Broker 内 round 2位）
    assert b.buy_fee(10_000) == pytest.approx(5.64, abs=0.005)
    assert b.sell_fee(10_000) == pytest.approx(15.64, abs=0.005)


def test_default_fee_structure():
    """默认费率结构：佣金万0.5(最低5元) + 印花税万5(卖出) + 经手万0.341 + 证管万0.2 + 过户万0.1"""
    b = Broker()
    amount = 200_000
    # 买单：佣金 max(10,5)=10 + 200000×万0.641=12.82 -> 22.82
    assert b.buy_fee(amount) == pytest.approx(10.0 + 12.82)
    # 卖单：佣金 10 + 印花税 100 + 12.82 -> 122.82
    assert b.sell_fee(amount) == pytest.approx(10.0 + 100.0 + 12.82)
    # 小单最低佣金（Broker 内 round 2位：5+0.3205=5.3205 -> 5.32）
    assert b.buy_fee(5_000) == pytest.approx(5.32, abs=0.005)


def test_backtest_fee_fields(demo_env):
    cfg, data_dir = make_config(demo_env)
    report = run_backtest(cfg, data_dir=data_dir)
    # 双边费用 = 过户万0.1 + 经手万0.341 + 证管万0.2 = 万0.641
    bilateral = 0.00001 + 0.0000341 + 0.00002
    for t in report["trade_log"]:
        expected = (max(t["amount"] * 0.0003, 5) + t["amount"] * bilateral)
        if t["side"] == "sell":
            expected += t["amount"] * 0.001
        assert t["fee"] == pytest.approx(expected, abs=0.5), \
            f"trade {t['trade_id']} 手续费不符: {t['fee']} vs {expected}"
    assert report["metrics"]["commission_total"] == pytest.approx(
        sum(t["fee"] for t in report["trade_log"]), abs=0.5)


# ---------------- 4. 涨跌停一字板不成交 ----------------

def _write_simple_daily(tmp_path, closes_by_code, gap_days=None):
    """构造日线 parquet：closes_by_code = {code: [(date, close)]}"""
    rows = []
    for code, items in closes_by_code.items():
        prev = None
        for date, c in items:
            o = items[0][1] if prev is None else prev
            hi, lo = max(o, c) * 1.005, min(o, c) * 0.995
            if gap_days and date in gap_days.get(code, {}):
                spec = gap_days[code][date]
                o = hi = lo = c = spec  # 一字板
            rows.append({"code": code, "date": date, "open": round(o, 2),
                         "high": round(hi, 2), "low": round(lo, 2),
                         "close": round(c, 2), "volume": 1_000_000,
                         "amount": round(c * 1_000_000, 2)})
            prev = c
    df = pl.DataFrame(rows)
    dates = sorted(df["date"].unique().to_list())
    store.write_daily(df, str(tmp_path))
    store.write_calendar(pl.DataFrame({"date": dates, "is_open": [1] * len(dates)}), str(tmp_path))
    store.write_adj_factor(pl.DataFrame({
        "code": [c for c, _ in closes_by_code.items() for _ in dates],
        "date": dates * len(closes_by_code),
        "adj_factor": [1.0] * (len(dates) * len(closes_by_code))}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": list(closes_by_code), "name": [f"测试股{c}" for c in closes_by_code],
        "st": [False] * len(closes_by_code),
        "list_date": ["20100101"] * len(closes_by_code)}), str(tmp_path))
    return dates[0], dates[-1]


def test_limit_up_one_word_board_blocks_buy(tmp_path):
    """金叉信号次日一字涨停 → 买入不成交"""
    from app.engine.strategies.ma_cross import MaCrossStrategy
    import numpy as np
    # 下跌→上涨，确保出现金叉；随后插入一字涨停日
    prices = [10.0 - 0.08 * i for i in range(12)] + [9.1 + 0.12 * i for i in range(1, 12)]
    dates = synthetic.trade_dates(len(prices))
    strat = MaCrossStrategy()
    probe = pl.DataFrame({
        "code": ["600000"] * len(prices), "date": dates,
        "open": prices, "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices], "close": prices,
        "volume": [1e6] * len(prices), "amount": [p * 1e6 for p in prices]})
    sig = strat.prepare({"600000": probe}, {"fast": 3, "slow": 5})["600000"]
    golden = [i for i, s in enumerate(sig["signal"].to_list()) if s == 1]
    assert golden, "构造的数据应产生金叉信号"
    g = golden[0]
    # 把信号次日改为一字涨停（+10%）
    limit_price = round(prices[g] * 1.10, 2)
    prices2 = list(prices)
    prices2[g + 1] = limit_price
    limit_map = {"600000": {dates[g + 1]: limit_price}}
    closes = {"600000": list(zip(dates, prices2))}
    start, end = _write_simple_daily(tmp_path, closes, gap_days=limit_map)
    cfg = {
        "name": "limit-up-test", "strategy_id": "ma_cross",
        "params": {"fast": 3, "slow": 5, "max_adds": 0},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 30},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "daily", "initial_capital": 1_000_000,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    buys = [t for t in report["trade_log"] if t["side"] == "buy"]
    assert not any(t["time"][:10] == dates[g + 1] for t in buys), "一字涨停日不应有买入成交"
    assert not any(t["time"][:10] == dates[g + 2] for t in buys), \
        "被一字板挡掉的信号不应顺延成交"


def test_limit_down_one_word_board_blocks_sell(tmp_path):
    """死叉卖出信号次日一字跌停 → 卖出不成交、仓位保留"""
    from app.engine.strategies.ma_cross import MaCrossStrategy
    # 下跌 → 上涨（金叉买入）→ 缓跌（触发死叉）
    prices = ([12.0 - 0.15 * i for i in range(8)]
              + [11.1 + 0.15 * i for i in range(1, 13)]
              + [11.8 - 0.3 * i for i in range(1, 4)])
    dates_full = synthetic.trade_dates(len(prices))
    strat = MaCrossStrategy()

    def probe(p_list):
        df = pl.DataFrame({
            "code": ["600000"] * len(p_list), "date": dates_full[:len(p_list)],
            "open": p_list, "high": [p * 1.005 for p in p_list],
            "low": [p * 0.995 for p in p_list], "close": p_list,
            "volume": [1e6] * len(p_list), "amount": [p * 1e6 for p in p_list]})
        return strat.prepare({"600000": df}, {"fast": 3, "slow": 5})["600000"]

    sig = probe(prices)
    dead = [i for i, s in enumerate(sig["signal"].to_list()) if s == -1]
    assert dead, "构造的数据应产生死叉信号"
    g = dead[0]
    # 序列截断到 g+1，并把 g+1 设为一字跌停（-10%），不影响此前信号
    prices2 = prices[:g + 1] + [round(prices[g] * 0.90, 2)]
    dates = dates_full[:g + 2]
    limit_map = {"600000": {dates[-1]: prices2[-1]}}
    closes = {"600000": list(zip(dates, prices2))}
    start, end = _write_simple_daily(tmp_path, closes, gap_days=limit_map)
    cfg = {
        "name": "limit-down-test", "strategy_id": "ma_cross",
        "params": {"fast": 3, "slow": 5, "max_adds": 0},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 30},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "daily", "initial_capital": 1_000_000,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    buys = [t for t in report["trade_log"] if t["side"] == "buy"]
    assert buys, "应已买入建仓"
    sells_on_limit_day = [t for t in report["trade_log"]
                          if t["side"] == "sell" and t["time"][:10] == dates[-1]]
    assert not sells_on_limit_day, "一字跌停日不应有卖出成交"
    last_snap = report["position_snapshots"][-1]
    assert last_snap["positions"], "卖出被一字跌停阻挡后仓位应保留"


def test_broker_limit_detect():
    b = Broker()
    up_bar = {"open": 11, "high": 11, "low": 11, "close": 11, "volume": 1}
    assert b.is_limit_up(up_bar, 10.0) is True
    assert b.is_limit_down(up_bar, 10.0) is False
    dn_bar = {"open": 9, "high": 9, "low": 9, "close": 9, "volume": 1}
    assert b.is_limit_down(dn_bar, 10.0) is True
    assert b.is_limit_up(dn_bar, 10.0) is False
    normal = {"open": 10, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 1}
    assert b.is_limit_up(normal, 10.0) is False
    assert b.is_limit_down(normal, 10.0) is False


# ---------------- 5. 做T分解（分钟级 grid_t） ----------------

def _write_volatile_minute_data(tmp_path, n_days=6):
    """构造高波动分钟数据：日内正弦 ±5%，确保网格多次触发做T"""
    codes = ["600000"]
    rows = []
    dates = synthetic.trade_dates(n_days)
    for code in codes:
        for di, d in enumerate(dates):
            base = 10.0 + di * 0.05
            closes = [base * (1 + 0.05 * math.sin(2 * math.pi * k / 12))
                      for k in range(len(synthetic.BAR_TIMES))]
            prev_c = base
            for k, hhmm in enumerate(synthetic.BAR_TIMES):
                c = closes[k]
                o = prev_c
                rows.append({
                    "code": code, "date": f"{d} {hhmm}",
                    "open": round(o, 3), "high": round(max(o, c) * 1.001, 3),
                    "low": round(min(o, c) * 0.999, 3), "close": round(c, 3),
                    "volume": 100_000, "amount": round(c * 100_000, 2)})
                prev_c = c
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
    store.write_adj_factor(pl.DataFrame({
        "code": [codes[0]] * len(rows), "date": mdf["date"].to_list(),
        "adj_factor": [1.0] * len(rows)}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": codes, "name": ["测试股600000"], "st": [False], "list_date": ["20100101"]}),
        str(tmp_path))
    return dates_all[0], dates_all[-1]


def test_grid_t_minute_t_trade_decomposition(tmp_path):
    start, end = _write_volatile_minute_data(tmp_path)
    cfg = {
        "name": "grid-t-test", "strategy_id": "grid_t",
        "params": {"base_pct": 30, "grid_atr_mult": 0.3, "atr_period": 2,
                   "max_t_times": 6, "max_adds": 0},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 60,
                        "max_total_position_pct": 100, "max_intraday_trades": 12},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "minute5", "initial_capital": 1_000_000,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    log = report["trade_log"]
    assert log, "应有成交记录"
    # 底仓开仓存在
    assert any(t["type"] == "开仓" for t in log), "应有底仓开仓记录"
    # 做T记录存在（T+1 真实约束：先卖旧再当日买回）
    assert any(t["type"] == "做T" and t["side"] == "sell" for t in log), "应有做T卖出记录"
    assert any(t["type"] == "做T" and t["side"] == "buy" for t in log), "应有做T买回记录"
    # 做T配对统计（当日卖旧+买回）
    assert report["metrics"]["t_trade_count"] > 0, "t_trade_count 应 > 0"
    assert isinstance(report["metrics"]["t_pnl"], (int, float))
    # 时间格式 YYYY-MM-DD HH:mm
    assert " " in log[0]["time"]
    # T+1：分钟级当日买入不可卖（做T买回的仓位当日无卖出）
    for t in log:
        if t["side"] == "sell":
            assert (t.get("open_time") or t["time"])[:10] != t["time"][:10]


# ---------------- 6. 风控：个股仓位上限 ----------------

def test_position_cap_per_stock(demo_env):
    # 单股 universe：个股市值占比不应超过 30%（+单bar成交误差容差）
    cfg2, data_dir = make_config(demo_env, universe=["600000"], risk_config={
        "max_position_pct_per_stock": 30, "max_total_position_pct": 100,
        "stop_loss_mode": "none", "max_drawdown_breaker": 30})
    r2 = run_backtest(cfg2, data_dir=data_dir)
    prices = {r["date"]: r["close"] for r in
              store.read_daily(["600000"], data_dir).to_dicts()}
    factor = 1.05  # 单bar成交误差容忍
    for s in r2["position_snapshots"]:
        for pos in s["positions"]:
            px = prices.get(s["date"], 0) or 0
            mv = pos["volume"] * px
            if s["market_value"] > 0 and mv > 0:
                ratio = mv / (s["cash"] + s["market_value"])
                assert ratio <= 0.30 * factor + 0.02, \
                    f"{s['date']} 个股仓位 {ratio:.2%} 超过上限30%"


def test_total_position_cap(demo_env):
    cfg, data_dir = make_config(demo_env, risk_config={
        "max_position_pct_per_stock": 30, "max_total_position_pct": 50,
        "stop_loss_mode": "none", "max_drawdown_breaker": 30})
    report = run_backtest(cfg, data_dir=data_dir)
    for p in report["equity_curve"]:
        assert p["position_ratio"] <= 0.50 * 1.05 + 0.01, \
            f"{p['date']} 总仓位 {p['position_ratio']:.2%} 超过上限50%"


# ---------------- 7. 持仓只数与现金缓冲 ----------------

def test_max_holdings(demo_env):
    """3只票 universe + max_holdings=1 -> 任意时点持仓股票数 <= 1"""
    cfg, data_dir = make_config(demo_env, risk_config={
        "max_position_pct_per_stock": 30, "max_total_position_pct": 100,
        "stop_loss_mode": "none", "max_drawdown_breaker": 30,
        "max_holdings": 1})
    report = run_backtest(cfg, data_dir=data_dir)
    for s in report["position_snapshots"]:
        n = len({p["code"] for p in s["positions"]})
        assert n <= 1, f"{s['date']} 持仓 {n} 只超过上限1"


def test_cash_reserve(demo_env):
    """cash_reserve_pct=40 -> 可投上限 60%：仓位比例恒 <= 60%（容差）"""
    cfg, data_dir = make_config(demo_env, risk_config={
        "max_position_pct_per_stock": 100, "max_total_position_pct": 100,
        "stop_loss_mode": "none", "max_drawdown_breaker": 30,
        "cash_reserve_pct": 40})
    report = run_backtest(cfg, data_dir=data_dir)
    for p in report["equity_curve"]:
        assert p["position_ratio"] <= 0.60 * 1.05 + 0.02, \
            f"{p['date']} 总仓位 {p['position_ratio']:.2%} 超过现金缓冲约束（应<=60%）"


# ---------------- 8. 月度出金 ----------------

def _write_trend_minute_data(tmp_path, n_days=90, daily_ret=0.004, amp=0.02):
    """缓慢趋势 + 日内正弦波动的分钟数据（自然跨约3-4个月）"""
    codes = ["600000"]
    rows = []
    dates = synthetic.trade_dates(n_days)
    for code in codes:
        base = 10.0
        for _di, d in enumerate(dates):
            closes = [base * (1 + amp * math.sin(2 * math.pi * k / 12))
                      for k in range(len(synthetic.BAR_TIMES))]
            prev_c = base
            for k, hhmm in enumerate(synthetic.BAR_TIMES):
                c = closes[k]
                o = prev_c
                rows.append({
                    "code": code, "date": f"{d} {hhmm}",
                    "open": round(o, 3), "high": round(max(o, c) * 1.001, 3),
                    "low": round(min(o, c) * 0.999, 3), "close": round(c, 3),
                    "volume": 100_000, "amount": round(c * 100_000, 2)})
                prev_c = c
            base *= (1 + daily_ret)
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
    store.write_adj_factor(pl.DataFrame({
        "code": [codes[0]] * len(rows), "date": mdf["date"].to_list(),
        "adj_factor": [1.0] * len(rows)}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": codes, "name": ["趋势股600000"], "st": [False],
        "list_date": ["20100101"]}), str(tmp_path))
    return dates_all[0], dates_all[-1]


def test_monthly_withdrawal(tmp_path):
    """出金开启：逐笔T提成 + 月末兜底；统计用调整净值（出金不算亏损）"""
    start, end = _write_trend_minute_data(tmp_path)
    cfg = {
        "name": "withdraw-test", "strategy_id": "grid_t",
        "params": {"base_pct": 60, "grid_atr_mult": 0.5, "atr_period": 3,
                   "max_t_times": 6, "max_adds": 0},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 90,
                        "max_total_position_pct": 100, "max_intraday_trades": 12},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "minute5", "initial_capital": 400_000,
        "monthly_withdraw_base": 5000, "t_profit_withdraw_pct": 10,
        "min_t_amount": 0,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    wd = report["withdrawal"]
    # 出金结构完整
    assert set(wd) >= {"monthly_base", "total", "t_profit", "month_topup", "months", "log"}
    assert wd["monthly_base"] == 5000
    # 跨月数据：应有多个月的出金记录
    assert len(wd["months"]) >= 2, "90天数据应覆盖>=2个自然月"
    # 至少发生了一笔出金（T提成或月末补齐）
    assert wd["total"] > 0, "盈利行情下应有出金"
    # 调整净值 >= 真实净值（累计提取 >= 0）
    for p in report["equity_curve"]:
        assert p["adjusted_equity"] >= p["equity"] - 0.01
    # 总收益基于调整净值：含已提取金额
    m = report["metrics"]
    assert m["withdrawn_total"] == pytest.approx(wd["total"])
    expected_ret = (m["end_equity"] + wd["total"]) / 400_000 - 1
    assert m["total_return"] == pytest.approx(expected_ret, abs=1e-4)
    # 出金覆盖率：有出金配置时应给出
    assert m["withdrawal_coverage"] is not None or wd["monthly_base"] == 0


def test_withdrawal_disabled_by_default(tmp_path):
    """出金默认关闭：行为与旧版一致，无出金记录且口径不变"""
    start, end = _write_trend_minute_data(tmp_path, n_days=30)
    cfg = {
        "name": "no-withdraw", "strategy_id": "grid_t",
        "params": {"base_pct": 60, "grid_atr_mult": 0.5, "atr_period": 3,
                   "max_t_times": 6, "max_adds": 0},
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 90,
                        "max_total_position_pct": 100, "max_intraday_trades": 12},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "minute5", "initial_capital": 400_000,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    assert report["withdrawal"]["total"] == 0
    assert report["metrics"]["withdrawn_total"] == 0
    for p in report["equity_curve"]:
        assert p["adjusted_equity"] == pytest.approx(p["equity"])


# ---------------- 9. momentum_t 策略 ----------------

def _write_noisy_minute_data(tmp_path, n_days=160, drift=0.001, noise=0.008,
                             crash_days=0, crash_ret=0.05, seed=1):
    """带噪声分钟数据：日收益 = drift + N(0, noise)，末段可选暴涨（崩溃保护测试用）
    噪声让 σ 处于真实量级（约1%），避免恒定收益序列触发 std 浮点下溢"""
    import numpy as np
    rng = np.random.default_rng(seed)
    codes = ["600000"]
    rows = []
    dates = synthetic.trade_dates(n_days)
    for code in codes:
        prev = 10.0
        for di, d in enumerate(dates):
            ret = crash_ret if (crash_days and di >= n_days - crash_days) \
                else drift + float(rng.normal(0, noise))
            for k, hhmm in enumerate(synthetic.BAR_TIMES):
                o = prev
                c = o * (1 + ret / 48)
                rows.append({"code": code, "date": f"{d} {hhmm}",
                             "open": round(o, 4), "high": round(max(o, c) * 1.001, 4),
                             "low": round(min(o, c) * 0.999, 4), "close": round(c, 4),
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
    store.write_adj_factor(pl.DataFrame({
        "code": [codes[0]] * len(rows), "date": mdf["date"].to_list(),
        "adj_factor": [1.0] * len(rows)}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": codes, "name": ["噪声趋势股600000"], "st": [False],
        "list_date": ["20100101"]}), str(tmp_path))
    return dates_all[0], dates_all[-1]


def test_momentum_t_bull_open_and_bear_no_open(tmp_path):
    """上涨趋势：出现开仓信号（带动态预算）；下跌趋势：不建仓
    数据需 >= mom_long(120)+缓冲 -> 用 160 天带噪声数据"""
    from app.engine.strategies.momentum_t import MomentumTStrategy
    strat = MomentumTStrategy()
    start, end = _write_noisy_minute_data(tmp_path, n_days=160, drift=0.006, seed=11)
    from app.engine import datafeed
    data = datafeed.load_minute5(["600000"], start, end, str(tmp_path))
    sig = strat.prepare(data, {}, start_date=start)["600000"]
    rows = sig.to_dicts()
    opens = [r for r in rows if r["signal"] == 1 and r["tag"] == "开仓"]
    assert opens, "上涨趋势应产生开仓信号"
    for r in opens:
        assert r["date"][:10] >= start, "开仓信号不应早于回测起始日（预热边界）"
        assert r["budget_pct"] is not None and 10 <= r["budget_pct"] <= 70
    # 下跌趋势数据：不建仓
    start2, end2 = _write_noisy_minute_data(tmp_path / "bear", n_days=160,
                                            drift=-0.006, seed=22)
    data2 = datafeed.load_minute5(["600000"], start2, end2, str(tmp_path / "bear"))
    sig2 = strat.prepare(data2, {}, start_date=start2)["600000"]
    assert not any(r["signal"] == 1 and r["tag"] == "开仓"
                   for r in sig2.to_dicts()), "下跌趋势不应开仓"


def test_momentum_crash_guard_sigma_adaptive():
    """σ自适应崩溃保护：平稳期有动量分；末段连续暴涨 -> 动量分作废（不入榜）
    σ 窗口包含暴涨日会被撑大（真实特性，保护存在1日滞后），断言后段置空"""
    import numpy as np
    from app.engine.strategies.momentum_t import MomentumTStrategy
    strat = MomentumTStrategy()
    dates = synthetic.trade_dates(160)
    n_days = len(dates)
    rows = []
    rng = np.random.default_rng(3)
    prev = 10.0
    for di, d in enumerate(dates):
        ret = 0.05 if di >= n_days - 5 else 0.001 + float(rng.normal(0, 0.008))
        for hhmm in synthetic.BAR_TIMES:
            o = prev
            c = o * (1 + ret / 48)
            rows.append({"code": "600000", "date": f"{d} {hhmm}",
                         "open": round(o, 4), "high": round(max(o, c) * 1.0005, 4),
                         "low": round(min(o, c) * 0.9995, 4), "close": round(c, 4),
                         "volume": 100_000, "amount": round(c * 100_000, 2)})
            prev = c
    df = pl.DataFrame(rows)
    p = {k["key"]: k["default"] for k in strat.param_schema}
    feats = strat._daily_features(df, p)
    days = feats["day"].to_list()
    scores = feats["score"].to_list()
    # 平稳期（长周期就绪后）：动量分非空且为正
    ready = [(d, s) for d, s in zip(days, scores) if d < dates[n_days - 5]]
    assert any(s is not None and s > 0 for _d, s in ready), "平稳上涨期应有正动量分"
    # 暴涨后段（第3~5天）：σ自适应保护触发，动量分置空
    crash = [s for d, s in zip(days, scores) if d >= dates[n_days - 3]]
    assert all(s is None for s in crash), "连续暴涨末段动量分应被崩溃保护置空"


def test_momentum_t_full_backtest(tmp_path):
    """momentum_t 完整回测：报告结构完整、生命周期交易类型合法"""
    start, end = _write_noisy_minute_data(tmp_path, n_days=160, drift=0.005, seed=33)
    cfg = {
        "name": "momentum-t-test", "strategy_id": "momentum_t",
        "params": {},  # 全默认参数
        "risk_config": {"stop_loss_mode": "none", "max_position_pct_per_stock": 100,
                        "max_total_position_pct": 100, "max_intraday_trades": 8,
                        "max_holdings": 3},
        "universe": ["600000"], "start_date": start, "end_date": end,
        "period": "minute5", "initial_capital": 400_000,
        "monthly_withdraw_base": 5000, "t_profit_withdraw_pct": 10,
    }
    report = run_backtest(cfg, data_dir=str(tmp_path))
    assert report["metrics"]["total_trades"] > 0, "应有交易发生"
    types = {t["type"] for t in report["trade_log"]}
    assert "开仓" in types, "应有开仓交易"
    # 信号列扩展字段驱动的能力：做T/加仓/减仓（若发生）必须类型合法
    assert types <= {"开仓", "加仓", "减仓", "做T", "止损", "止盈", "清仓"}
    for t in report["trade_log"]:
        if t["side"] == "sell":
            assert (t.get("open_time") or "")[:10] != t["time"][:10], "T+1违规"


def test_momentum_t_warmup_no_trades_before_start(tmp_path):
    """预热期前推：start_date 之前不产生任何交易"""
    start, end = _write_trend_minute_data(tmp_path, n_days=60, daily_ret=0.005)
    cfg = {
        "name": "warmup-test", "strategy_id": "momentum_t",
        "params": {}, "risk_config": {"stop_loss_mode": "none"},
        "universe": ["600000"], "start_date": end, "end_date": end,
        "period": "minute5", "initial_capital": 400_000,
    }
    # start_date=end_date（同一天）：warmup_days=300 前推加载，仅末日交易
    # 数据窗口整体处于预热期内时不崩溃
    report = run_backtest(cfg, data_dir=str(tmp_path))
    assert "metrics" in report
    for t in report["trade_log"]:
        assert t["time"][:10] >= end
