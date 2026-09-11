# -*- coding: utf-8 -*-
"""做T层重验前置检查：5分钟线在 v5 区间(2022-09-11~2026-09-10)的覆盖统计。"""
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.stage0_anchors import _zz500_universe, START_DEFAULT  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data" / "minute5"

# 列裁剪扫描（1.5GB 全表只取 code/date 两列）
lf = pl.scan_parquet(str(DATA / "*.parquet"))
idx = lf.select(["code", "date"]).collect()
codes = idx["code"].unique().to_list()
print("minute5 覆盖股票数:", len(codes))
dmin, dmax = idx["date"].min(), idx["date"].max()
print("日期范围:", dmin, "~", dmax)

start = (idx.sort(["code", "date"]).group_by("code").first()
         .with_columns(pl.col("date").str.slice(0, 7).alias("ym")))
print("起始月份分布 Top8:")
for r in start["ym"].value_counts().sort("ym").head(8).iter_rows(named=True):
    print(f"  {r['ym']}: {r['count']} 只")

u500 = set(_zz500_universe(START_DEFAULT))
print(f"\nv5 域 500 只中分钟线覆盖: {len(u500 & set(codes))}/500")
missing = sorted(u500 - set(codes))
if missing:
    print("缺分钟线的:", missing[:10], "..." if len(missing) > 10 else "")

in_rng = idx.filter((pl.col("date") >= f"{START_DEFAULT} 09:00") &
                    (pl.col("date") <= "2026-09-10 15:00"))
per = in_rng.group_by("code").len()
print(f"v5 区间内分钟 bar: 总 {in_rng.height:,} 行 | 中位/股 {per['len'].median():.0f} | "
      f"p10 {per['len'].quantile(0.1):.0f} | 最少 {per['len'].min()} 只")
