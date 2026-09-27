# -*- coding: utf-8 -*-
"""补充分析：其他 4294 码的日K起点精确分布 + 缺行日期分布 + 因子缺口构成"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

START = "2021-01-01"
daily_all = store.read_daily(None, str(ROOT / "data"))
END = str(daily_all["date"].max())
all_codes = sorted(daily_all["code"].unique().to_list())
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
zz500 = set(hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
            ["code"].unique().to_list())
others = [c for c in all_codes if c not in zz500]
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]

have_map = {r["code"]: set(r["ds"]) for r in
            (daily_all.filter((pl.col("date") >= START) & (pl.col("date") <= END))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}

# 1) 起点（年-月）分布
starts = Counter(min(have_map[c])[:7] for c in others if have_map.get(c))
print("[其他·起点年-月分布]:", flush=True)
for k, v in sorted(starts.items()):
    print(f"    {k}: {v}", flush=True)

# 2) 缺行日期分布
all_miss = Counter()
for c in others:
    have = have_map.get(c)
    if not have:
        continue
    f, l = min(have), max(have)
    for d in cal_dates:
        if f <= d <= l and d not in have:
            all_miss[d] += 1
print(f"\n[其他·缺行] 合计 {sum(all_miss.values())} 天", flush=True)
print("  按年:", dict(sorted(Counter(d[:4] for d in all_miss).items())), flush=True)
top = sorted(all_miss.items(), key=lambda x: -x[1])[:12]
print("  缺码最多日期:", [(d, n) for d, n in top], flush=True)

# 3) 因子缺口构成
adj = store.read_adj_factor(None, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
fg = {}
for c in others:
    have = have_map.get(c, set())
    diff = {d for d in have - a_map.get(c, set()) if d >= START}
    if diff:
        fg[c] = sorted(diff)
print(f"\n[其他·因子] {len(fg)} 码 / {sum(len(v) for v in fg.values())} 天", flush=True)
for c, v in sorted(fg.items(), key=lambda x: -len(x[1]))[:15]:
    print(f"    {c}: {len(v)} 天 ({v[0]}~{v[-1]})", flush=True)
