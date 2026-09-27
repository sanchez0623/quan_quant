# -*- coding: utf-8 -*-
"""验证 473 码因子差行是否全部为 2026-09-07 一天"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
d_map = {r["code"]: set(r["ds"]) for r in
         daily.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

diff_days = Counter()
n_less = 0
for c, have in d_map.items():
    a = a_map.get(c, set())
    diff = have - a
    if diff:
        n_less += 1
        for d in diff:
            diff_days[d] += 1
print(f"因子行数少于日K的码数: {n_less}", flush=True)
print("缺失日分布:", dict(sorted(diff_days.items())), flush=True)

# 反向：因子有而日K没有的（异常）
rev = 0
for c, a in a_map.items():
    if a - d_map.get(c, set()):
        rev += 1
print(f"反向(因子有日K无)码数: {rev}", flush=True)
