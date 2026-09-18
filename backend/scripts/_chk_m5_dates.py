# -*- coding: utf-8 -*-
"""诊断：minute5 全库最后日期分布（验证 09-17 数据在库、09-18 半日覆盖面）"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

DATA = Path(r"d:\Sanchez\AI\TraeProjects\quan_quant\data\minute5")
files = sorted(DATA.glob("*.parquet"))


def _stat(fp: Path):
    try:
        df = pl.scan_parquet(fp).select(
            pl.col("date").min().alias("mn"),
            pl.col("date").max().alias("mx"),
            pl.len().alias("n")).collect()
        return fp.stem, str(df["mn"][0]), str(df["mx"][0]), int(df["n"][0])
    except Exception:
        return fp.stem, None, None, 0


with ThreadPoolExecutor(8) as ex:
    stats = list(ex.map(_stat, files))

dist = Counter(mx[:10] for _, _, mx, _ in stats if mx)
print(f"文件总数: {len(stats)}")
print("最后日期分布（众数=对齐日）:")
for d, n in sorted(dist.items(), reverse=True):
    print(f"  {d}: {n} 个文件")

# 抽查一个 09-18 半日文件的具体截止时刻
for stem, mn, mx, n in stats:
    if mx and mx[:10] == "2026-09-18":
        print(f"\n抽查半日文件 {stem}: {mn} -> {mx}  ({n} 行)")
        break
# 抽查一个 09-17 完整文件
for stem, mn, mx, n in stats:
    if mx and mx[:10] == "2026-09-17":
        print(f"抽查完整文件 {stem}: {mn} -> {mx}  ({n} 行)")
        break
