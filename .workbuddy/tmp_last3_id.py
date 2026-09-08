# -*- coding: utf-8 -*-
"""核实剩余 3 段：302132 身份 + 002920/688065 当日 vol 具体值"""
import polars as pl
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store

# 302132 身份：stock_basic + 历史快照
basic = store.read_stock_basic(str(ROOT / "data"))
b = basic.filter(pl.col("code") == "302132")
if b.height:
    print("302132 stock_basic:", b.to_dicts())
else:
    print("302132 不在 stock_basic")

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
h = hist.filter(pl.col("code") == "302132")
if h.height:
    print("302132 历史快照:", h.select(["snap_date", "name"]).to_dicts()[:5], "共", h.height, "条")
else:
    print("302132 不在历史快照")

# 002920 / 688065 当日 vol
daily = store.read_daily(None, str(ROOT / "data"))
for c, d in [("002920", "2022-02-21"), ("688065", "2023-06-15")]:
    r = daily.filter((pl.col("code") == c) & (pl.col("date") == d))
    if r.height:
        x = r.to_dicts()[0]
        print(f"{c} {d}: open={x['open']} high={x['high']} low={x['low']} close={x['close']} "
              f"vol={x['volume']} amount={x['amount']}")
    else:
        print(f"{c} {d}: 无日线行")
