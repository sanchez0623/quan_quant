# -*- coding: utf-8 -*-
"""核验：新10在 stock_basic？日K行数？"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store

DATA = str(ROOT / "data")
NEW10 = ["601091", "601123", "603448", "688801", "688837",
         "301686", "301688", "301689", "301697", "301699"]

basic = store.read_stock_basic(DATA)
print(f"stock_basic: {basic.height} 行", flush=True)
for c in NEW10:
    r = basic.filter(pl.col("code") == c)
    if r.height:
        x = r.to_dicts()[0]
        print(f"  {c}: name={x['name']} list_date={x['list_date']} delisted={x['delisted']}", flush=True)
    else:
        print(f"  {c}: 不在 stock_basic！", flush=True)

daily_all = store.read_daily(NEW10, DATA)
agg = (daily_all.group_by("code").agg(pl.col("date").count().alias("n"),
                                      pl.col("date").min().alias("first"),
                                      pl.col("date").max().alias("last")).sort("code"))
for r in agg.to_dicts():
    print(f"  日K {r['code']}: {r['n']} 行 {r['first']}~{r['last']}", flush=True)
missing = [c for c in NEW10 if c not in set(agg["code"].to_list())]
print(f"日K缺失: {missing}", flush=True)
