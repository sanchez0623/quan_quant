# -*- coding: utf-8 -*-
"""验证剩余 3 段：daily vol 真实性 + mootdx 可拉性"""
import polars as pl
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import MootdxSource

daily = store.read_daily(None, str(ROOT / "data"))

# 1) daily 里 302132 全貌
for c in ["302132", "002920", "688065"]:
    sub = daily.filter(pl.col("code") == c).sort("date")
    print(f"\n{c}: 总行数={sub.height}, 起={sub['date'].min() if sub.height else '-'}, "
          f"止={sub['date'].max() if sub.height else '-'}")
    if sub.height:
        v = sub["volume"].fill_null(0)
        print(f"   vol>0 行数: {(v > 0).sum()} | vol=0行: {(v == 0).sum()}")
        # 打印 vol>0 的日期范围
        pos = sub.filter(pl.col("volume").fill_null(0) > 0)
        if pos.height:
            print(f"   vol>0 日期: {pos['date'].min()} ~ {pos['date'].max()} ({pos.height} 天)")

# 2) mootdx 试拉 302132（退市股 TDX 可能无）
md = MootdxSource()
for c, s, e in [("302132", "2023-03-27", "2024-08-09"),
                ("002920", "2022-02-21", "2022-02-21"),
                ("688065", "2023-06-15", "2023-06-15")]:
    df = md.get_minute5(c, s, e)
    if df is None or df.height == 0:
        print(f"\n[mootdx 空] {c} {s}~{e}", flush=True)
    else:
        print(f"\n[mootdx OK] {c} {s}~{e}: {df.height} bar {df['date'].min()} ~ {df['date'].max()}", flush=True)
