# -*- coding: utf-8 -*-
"""351 码缺行的日期分布：是否集中 2026-09-08 之后"""
import json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys_path = str(ROOT / "backend")
import sys
sys.path.insert(0, sys_path)
from app import config  # noqa: F401
from app.data import store

daily_all = store.read_daily(None, str(ROOT / "data"))
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if "2021-01-01" <= d <= str(daily_all["date"].max())]

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = set(seg["code"].unique().to_list())

dsub = daily_all.filter(pl.col("code").is_in(codes))
have_map = {r["code"]: set(r["ds"]) for r in
            dsub.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

all_miss = Counter()
per_code = {}
for c, have in have_map.items():
    f, l = min(have), max(have)
    miss = [d for d in cal_dates if f <= d <= l and d not in have]
    if miss:
        per_code[c] = miss
        for d in miss:
            all_miss[d] += 1

print(f"缺行码数: {len(per_code)} / 总缺 {sum(len(v) for v in per_code.values())} 天", flush=True)
# 按月分布
bymonth = Counter(d[:7] for d in all_miss)
print("缺口按月分布:", dict(sorted(bymonth.items())), flush=True)
# 2026-09 之后的天
sep = {d: n for d, n in all_miss.items() if d >= "2026-09-08"}
print(f"\n2026-09-08 后缺口: {sum(sep.values())} 天, 日期明细:", dict(sorted(sep.items())), flush=True)
# 2026-09-08 前的缺口码数
old = {c: [d for d in v if d < "2026-09-08"] for c, v in per_code.items()}
old = {c: v for c, v in old.items() if v}
print(f"2026-09-08 前缺行的码数: {len(old)} / 天数 {sum(len(v) for v in old.values())}", flush=True)
