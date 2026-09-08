# -*- coding: utf-8 -*-
"""查证：单日缺口段在日线库 vs baostock 5分钟原始返回"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import BaostockSource

cases = [
    ("600804", "2024-04-30"),
    ("000046", "2023-05-04"),
    ("600823", "2023-05-04"),
    ("688065", "2023-06-15"),
    ("002002", "2023-10-11"),
    ("300010", "2026-04-30"),
    ("000540", "2023-05-04"),
]

daily = store.read_daily(None, str(ROOT / "data"))
for c, d in cases:
    row = daily.filter((pl.col("code") == c) & (pl.col("date") == d))
    print(f"{c} {d}: 日线行数={row.height}", flush=True)
    if row.height:
        r = row.to_dicts()[0]
        print(f"    close={r['close']} vol={r['volume']} amount={r['amount']}", flush=True)

# baostock 原始返回（不过滤）看看到底返回了什么
bs = BaostockSource()
raw_cases = [
    ("600804", "2024-04-30", "2024-04-30"),
    ("000046", "2023-05-01", "2023-05-10"),   # 放大区间看是否有数据
    ("002002", "2023-10-09", "2023-10-13"),
    ("600000", "2021-01-04", "2021-01-08"),
]
for name, c, s, e in raw_cases:
    df = bs.get_minute5(c, s, e)
    if df is None:
        print(f"[baostock空] {c} {s}~{e}", flush=True)
    else:
        days = df["date"].str.slice(0, 10).unique().to_list()
        print(f"[baostock OK] {c} {s}~{e}: {df.height} bar, {len(days)} 个日期, "
              f"{df['date'].min()} ~ {df['date'].max()}", flush=True)
