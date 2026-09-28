# -*- coding: utf-8 -*-
"""dragon_dip 情绪门控真值诊断：复刻 runner 喂数据路径，捕获 regime 门控，
统计 gate_off 触发天数与开仓信号被拦截比例。"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app.engine import datafeed, runner
from app.engine.strategies import REGISTRY, apply_param_defaults
from app.engine.strategies.dragon_dip import DragonDipStrategy

START, END = "2022-09-01", "2026-09-24"
basic = __import__("app.data.store", fromlist=["store"]).read_stock_basic()
universe = basic.filter(~pl.col("delisted"))["code"].to_list()
universe = runner._filter_st(universe, True, None)
warmup = DragonDipStrategy.warmup_days
load_start = runner._shift_back(START, warmup)

print(f"universe {len(universe)}，load {load_start}~{END} …", flush=True)
data = datafeed.load_daily(universe, load_start, END, None)
print(f"data {len(data)} 码", flush=True)

strategy = REGISTRY["dragon_dip"]
params = apply_param_defaults("dragon_dip", {})

captured = {}
orig_rank = DragonDipStrategy._rank_entries


def rank_spy(work, regime, top_n):
    captured["regime"] = regime
    n_sig = 0
    sig_days = set()
    for w in work.values():
        sigs = w.filter(pl.col("_entry"))
        n_sig += sigs.height
        sig_days.update(sigs["date"].to_list())
    captured["n_sig"] = n_sig
    captured["sig_days"] = sig_days
    allowed = orig_rank(work, regime, top_n)
    captured["allowed_n"] = sum(len(v) for v in allowed.values())
    return allowed


DragonDipStrategy._rank_entries = staticmethod(rank_spy)
strategy.prepare(data, params, start_date=START)
DragonDipStrategy._rank_entries = staticmethod(orig_rank)

regime = captured["regime"]
inwin = regime.filter(pl.col("date") >= START)
n_off = inwin.filter(pl.col("gate_off")).height
print(f"\n窗口 {START}~{END} 共 {inwin.height} 个交易日")
print(f"情绪门控 gate_off=True: {n_off} 天 ({n_off / inwin.height:.1%})")
print(f"euphoria=True: {inwin.filter(pl.col('euphoria')).height} 天 "
      f"({inwin.filter(pl.col('euphoria')).height / inwin.height:.1%})")

per_year = inwin.with_columns(pl.col("date").str.slice(0, 4).alias("y")) \
    .group_by("y").agg([pl.len().alias("days"),
                        pl.col("gate_off").sum().alias("gate_off"),
                        pl.col("euphoria").sum().alias("euphoria")]).sort("y")
print("\n分年触发：")
print(per_year)

print(f"\n原始开仓信号（code,date 对）: {captured['n_sig']} 个，"
      f"覆盖 {len(captured['sig_days'])} 个交易日")
print(f"门控+top_n 后放行: {captured['allowed_n']} 个 "
      f"({captured['allowed_n'] / max(captured['n_sig'], 1):.1%})")
