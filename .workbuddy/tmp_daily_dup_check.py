# -*- coding: utf-8 -*-
"""验证 daily 表 (code,date) 重复行：多少码、重复内容是否一致、样例"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
dup = (daily.group_by(["code", "date"]).agg(pl.len().alias("n"))
       .filter(pl.col("n") > 1).sort("n", descending=True))
print(f"重复 (code,date) 组数: {dup.height}", flush=True)
print("涉及码数:", dup["code"].n_unique(), flush=True)

# 全库层面重复（不只 967 码）
daily_all = store.read_daily(None, str(ROOT / "data"))
dup_all = (daily_all.group_by(["code", "date"]).agg(pl.len().alias("n"))
           .filter(pl.col("n") > 1))
print(f"\n全库重复 (code,date) 组数: {dup_all.height} | 涉及码数: {dup_all['code'].n_unique()}", flush=True)

# 样例：看重复行内容差异
if dup_all.height:
    sample = dup_all.head(3).to_dicts()
    for s in sample:
        c, d = s["code"], s["date"]
        rows = daily_all.filter((pl.col("code") == c) & (pl.col("date") == d)).to_dicts()
        print(f"\n{c} {d} x{s['n']}:", flush=True)
        for r in rows:
            print(f"   open={r['open']} high={r['high']} low={r['low']} close={r['close']} "
                  f"vol={r['volume']} amt={r['amount']}", flush=True)
