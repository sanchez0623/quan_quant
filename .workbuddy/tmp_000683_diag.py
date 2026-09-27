# -*- coding: utf-8 -*-
"""诊断 000683：日K/因子日期数与差集 + 全库最新日期"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

daily_all = store.read_daily(None, str(ROOT / "data"))
print("全库日K最新日期:", daily_all["date"].max(), flush=True)
print("全库日K码数:", daily_all["code"].n_unique(), flush=True)

d = daily_all.filter(pl.col("code") == "000683").sort("date")
print(f"\n000683 日K: {d.height} 行, {d['date'].min()} ~ {d['date'].max()}", flush=True)
print("末 3 行:", d.tail(3).select(["date", "close"]).to_dicts(), flush=True)

adj = store.read_adj_factor(["000683"], str(ROOT / "data"))
a = adj.filter(pl.col("code") == "000683").sort("date")
print(f"\n000683 因子: {a.height} 行, {a['date'].min()} ~ {a['date'].max()}", flush=True)
print("末 3 行:", a.tail(3).to_dicts(), flush=True)

d_dates = set(d["date"].to_list())
a_dates = set(a["date"].to_list())
print(f"\n日K有因子无: {sorted(d_dates - a_dates)[:5]} (共{len(d_dates - a_dates)})", flush=True)
print(f"因子有日K无: {sorted(a_dates - d_dates)[:5]} (共{len(a_dates - d_dates)})", flush=True)
