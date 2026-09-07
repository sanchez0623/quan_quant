# -*- coding: utf-8 -*-
"""验证：660 码在 2021-01-01~2023-03-27 段的日K覆盖"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store

codes = (ROOT / ".workbuddy" / "zz500_2021_need.txt").read_text(encoding="utf-8").splitlines()
codes = [c for c in codes]
START, END = "2021-01-01", "2023-03-27"

daily = store.read_daily(None, str(ROOT / "data"))
sub = daily.filter(pl.col("code").is_in(codes)
                   & (pl.col("date") >= START) & (pl.col("date") <= END))
agg = sub.group_by("code").agg(
    pl.col("date").count().alias("n"),
    pl.col("date").min().alias("first"),
    pl.col("date").max().alias("last"),
)
got = set(agg["code"].to_list())
missing = [c for c in codes if c not in got]
print(f"660 码中已有 2021-01~2023-03-27 数据: {len(got)} | 完全缺: {len(missing)}")
if missing:
    print("  缺:", missing)

# 起点晚于 2021-01-01 的码（可能是 IPO 晚或拉取缺口）
late = agg.filter(pl.col("first") > "2021-01-15").sort("first")
print(f"\n首日 > 2021-01-15 的码: {late.height}")
print("  分布(月):", dict(sorted(Counter(r[:7] for r in late["first"].to_list()).items())))
if late.height <= 40:
    for r in late.to_dicts():
        print(f"    {r['code']}: n={r['n']}, first={r['first']}, last={r['last']}")

# 4 只退市股
print("\n退市股覆盖:")
for c in ["002013", "600068", "600260", "600291"]:
    r = agg.filter(pl.col("code") == c)
    if r.height:
        print(f"  {c}: n={r['n'][0]}, first={r['first'][0]}, last={r['last'][0]}")
    else:
        print(f"  {c}: 缺")

# 全库校验：任意码 2021-01~2023-03 段有「内部日期断档」的粗略检查（交易日本该连续，退市/停牌除外）
print(f"\n全库码数: {daily['code'].n_unique()}")
print(f"660 码段内最小行数: {agg['n'].min()}（<300 的码数: {(agg['n'] < 300).sum()}）")
small = agg.filter(pl.col("n") < 300).sort("n")
for r in small.to_dicts():
    print(f"  行数偏少 {r['code']}: n={r['n']}, first={r['first']}, last={r['last']}")
