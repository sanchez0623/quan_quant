# -*- coding: utf-8 -*-
"""adj_factor 补充证据：2021-2026 年份占比 + 2026-08-31 断崖方向 + 2023-03-28 名单特征。"""
import polars as pl
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
a = pl.read_parquet(DATA / "adj_factor.parquet").sort(["code", "date"])
a = a.with_columns(pl.col("date").str.slice(0, 4).alias("yr"),
                   (pl.col("adj_factor") == 1.0).alias("is1"))
g = a.group_by("yr").agg(pl.len().alias("n"), pl.col("is1").sum().alias("n1"))
print("adj_factor==1.0 占比按年份（近年）:")
for r in g.sort("yr").iter_rows(named=True):
    if r["yr"] >= "2020":
        print(f"  {r['yr']}: {r['n1']}/{r['n']} = {r['n1']/max(1, r['n']):.1%}")

# 2026-08-31 断崖方向
d = pl.read_parquet(DATA / "daily.parquet")
for c in ["300144", "000062", "605117"]:
    aa = a.filter((pl.col("code") == c) &
                  (pl.col("date") >= "2026-08-27") &
                  (pl.col("date") <= "2026-09-07")).sort("date")
    dd = d.filter((pl.col("code") == c) &
                  (pl.col("date") >= "2026-08-27") &
                  (pl.col("date") <= "2026-09-07")).sort("date")
    print(f"\n== {c} 2026-08-31 前后 ==")
    for r in dd.iter_rows(named=True):
        adj = aa.filter(pl.col("date") == r["date"])["adj_factor"].to_list()
        print(f"  {r['date']} close {r['close']:>8.2f} adj {adj[0] if adj else None}")

# 2023-03-28 断崖 283 只：行业/代码分布特征
bad = a.filter((pl.col("date") == "2023-03-28"))
prev = a.filter((pl.col("date") == "2023-03-27")).select(["code", "adj_factor"])
j = bad.join(prev, on="code", suffix="_prev").with_columns(
    (pl.col("adj_factor") / pl.col("adj_factor_prev")).alias("ratio"))
broken = j.filter((pl.col("ratio") < 0.7) | (pl.col("ratio") > 1.43))
print(f"\n2023-03-28 factor 归1/断崖的股票数: {broken.height}")
print("抽样 15 只:", broken["code"].to_list()[:15])
print("前值分布 p25/50/75:", broken["adj_factor_prev"].quantile(0.25),
      broken["adj_factor_prev"].median(), broken["adj_factor_prev"].quantile(0.75))
