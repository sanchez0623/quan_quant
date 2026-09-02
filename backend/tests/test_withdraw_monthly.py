# -*- coding: utf-8 -*-
"""月度出金回归测试：monthly_withdraw_base>0 时月末兜底必须执行（600339/微调2 案例）。

背景：bt_42037faf88f2 配置 monthly_withdraw_base=20000，20 个月 withdrawal.total=0
且 log 全空（每月末现金 5 万~208 万、浮盈 51 万~152 万，topup/shortfall 均应发生）。
"""
import pytest

from app.data import store, synthetic
from app.engine.runner import run_backtest


def _write_market(tmp_path, n_days=170, drift=0.006, seed=11):
    """稳赚分钟线（复用 test_engine 的合成模式）：drift>0 保证浮盈存在"""
    import numpy as np
    import polars as pl
    rng = np.random.default_rng(seed)
    rows = []
    dates = synthetic.trade_dates(n_days)
    prev = 10.0
    for d in dates:
        ret = drift + float(rng.normal(0, 0.004))
        for hhmm in synthetic.BAR_TIMES:
            o = prev
            c = o * (1 + ret / 48)
            rows.append({"code": "600000", "date": f"{d} {hhmm}",
                         "open": round(o, 4), "high": round(max(o, c) * 1.001, 4),
                         "low": round(min(o, c) * 0.998, 4), "close": round(c, 4),
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
               pl.lit("600000").alias("code"))
           .with_columns(pl.col("day").alias("date")).drop("day"))
    store.write_minute5("600000", mdf, str(tmp_path))
    store.write_daily(ddf, str(tmp_path))
    dates_all = sorted(ddf["date"].to_list())
    store.write_calendar(pl.DataFrame({"date": dates_all,
                                       "is_open": [1] * len(dates_all)}), str(tmp_path))
    store.write_adj_factor(pl.DataFrame({
        "code": ["600000"] * len(rows), "date": mdf["date"].to_list(),
        "adj_factor": [1.0] * len(rows)}), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": ["600000"], "name": ["测试股"], "st": [False],
        "list_date": ["20100101"]}), str(tmp_path))
    return dates_all[0], dates_all[-1]


def test_monthly_withdraw_executes(tmp_path):
    """wd_base=20000 + 稳赚策略：月末兜底应产生 topup 提取（log 非空、total>0）"""
    start, end = _write_market(tmp_path)
    cfg = {"name": "wd-test", "strategy_id": "momentum_t", "period": "minute5",
           "params": {}, "risk_config": {}, "universe": ["600000"],
           "monthly_withdraw_base": 20000.0, "t_profit_withdraw_pct": 10.0,
           "min_t_amount": 20000.0,
           "start_date": start, "end_date": end,
           "initial_capital": 1_000_000.0, "exclude_st": True}
    report = run_backtest(cfg, data_dir=str(tmp_path))
    wd = report.get("withdrawal") or {}
    assert (wd.get("monthly_base") or 0) == 20000.0, "出金目标应写入汇总"
    assert (wd.get("total") or 0) > 0, (
        f"稳赚 170 天且月末现金充足，月末兜底应已提取，"
        f"实际 total={wd.get('total')} log={len(wd.get('log') or [])} 条")
    types = {e["type"] for e in (wd.get("log") or [])}
    assert types & {"month_topup", "t_profit"}, f"应存在出金流水，实际类型: {types}"


def test_monthly_withdraw_zero_base_no_op(tmp_path):
    """wd_base=0（默认）：不做任何出金，log 为空"""
    start, end = _write_market(tmp_path / "b", seed=12)
    cfg = {"name": "wd-zero", "strategy_id": "momentum_t", "period": "minute5",
           "params": {}, "risk_config": {}, "universe": ["600000"],
           "start_date": start, "end_date": end,
           "initial_capital": 1_000_000.0, "exclude_st": True}
    report = run_backtest(cfg, data_dir=str(tmp_path / "b"))
    wd = report.get("withdrawal") or {}
    assert (wd.get("total") or 0) == 0 and not (wd.get("log") or [])


def test_monthly_withdraw_universe_auto(tmp_path):
    """universe_auto 分段路径（用户场景 bt_42037faf88f2）：
    月度出金必须穿透分段拼接，出现在最终报告的 withdrawal/metrics 里"""
    import numpy as np
    import polars as pl
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
    cfg = {"name": "wd-auto", "strategy_id": "momentum_t", "params": {},
           "risk_config": {}, "universe": [], "universe_auto": True,
           "auto_idle_days": 3, "auto_top_x": 2, "auto_above_ma": 60,
           "monthly_withdraw_base": 20000.0, "t_profit_withdraw_pct": 10.0,
           "min_t_amount": 20000.0,
           "start_date": dates[200], "end_date": dates[n - 1],
           "period": "daily", "initial_capital": 1_000_000.0, "exclude_st": True}
    report = run_backtest(cfg, data_dir=str(tmp_path))
    wd = report.get("withdrawal") or {}
    # 段内稳赚（200 日 +43% 级别）且月末有现金：兜底必须提取
    assert (wd.get("total") or 0) > 0, (
        f"universe_auto 分段下月度出金丢失！total={wd.get('total')} "
        f"log={len(wd.get('log') or [])} 条")
    m = report.get("metrics") or {}
    assert (m.get("withdrawn_total") or 0) > 0, "metrics.withdrawn_total 应同步"
