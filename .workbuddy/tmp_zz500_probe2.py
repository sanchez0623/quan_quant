# -*- coding: utf-8 -*-
"""抽查：mootdx 对 zz500 2021 段日K的覆盖 + 退市股支持 + 因子表缺口"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import MootdxSource, BaostockSource

START, END = "2021-01-01", "2023-03-27"

codes = (ROOT / ".workbuddy" / "zz500_2021_need.txt").read_text(encoding="utf-8").splitlines()
print(f"需补码数: {len(codes)}", flush=True)

# 1) mootdx 抽查（含退市股 002013/600068/600260/600291）
md = MootdxSource()
print(f"mootdx available: {md.available()}", flush=True)
for c in ["600000", "000002", "002013", "600068", "600260", "600291", "688001"]:
    df = md.get_daily(c, START, END)
    if df is None:
        print(f"  {c}: mootdx=None", flush=True)
    else:
        print(f"  {c}: {df.height} bars, first={df['date'].min()}, last={df['date'].max()}", flush=True)

# 2) baostock 抽查（同一批，验证覆盖）
bs = BaostockSource()
for c in ["002013", "600068", "600260", "600291"]:
    df = bs.get_daily(c, START, END)
    if df is None:
        print(f"  {c}: baostock=None", flush=True)
    else:
        print(f"  {c}: bs {df.height} bars, first={df['date'].min()}, last={df['date'].max()}", flush=True)

# 3) adj_factor 覆盖缺口：660 码中因子首日 > 2021-01-01 的码数与分布
adj = store.read_adj_factor(None, str(ROOT / "data"))
if adj is not None:
    first = adj.filter(pl.col("code").is_in(codes)).group_by("code").agg(
        pl.col("date").min().alias("first"))
    gap = first.filter(pl.col("first") > START)
    print(f"\n因子表: 660 码中 {first.height} 码有因子；"
          f"因子首日 > 2021-01-01 的 {gap.height} 码", flush=True)
    from collections import Counter
    print("  因子首日分布:", dict(sorted(Counter(r[:7] for r in gap["first"].to_list()).items())), flush=True)
    (ROOT / ".workbuddy" / "zz500_2021_adj_need.txt").write_text(
        "\n".join(gap["code"].to_list()), encoding="utf-8")
    print("  缺因子清单已存 zz500_2021_adj_need.txt", flush=True)
else:
    print("\nadj_factor 表不存在", flush=True)
