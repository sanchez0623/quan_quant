# -*- coding: utf-8 -*-
"""补充确认：269 个晚起点码的构成（IPO vs 缺历史段）+ 37 缺行码明细 + 因子差行明细"""
import sys, pathlib, json
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

START, END = "2021-01-01", "2026-09-07"
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= START))
codes = sorted(seg["code"].unique().to_list())
code_set = set(codes)

daily = store.read_daily(codes, str(ROOT / "data"))
g = (daily.group_by("code").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"))
     .filter(pl.col("code").is_in(code_set)))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list()))

basic = store.read_stock_basic(str(ROOT / "data"))
list_date = dict(zip(basic["code"].to_list(), basic["list_date"].to_list())) if "list_date" in basic.columns else {}

# --- 269 码分类 ---
late = [(c, first_map[c]) for c in codes if first_map.get(c, "9999") > "2021-01-04"]
ipo_natural, old_missing = [], []
for c, f in late:
    ld = list_date.get(c)
    if ld and str(ld) > "2021-01-01":
        ipo_natural.append(c)          # 2021 后上市：自然起点
    else:
        old_missing.append((c, f, ld))  # 2021 前已上市但数据起点晚：缺历史段
print(f"晚起点 {len(late)} 码 = IPO 自然起点 {len(ipo_natural)} + 老股票缺历史段 {len(old_missing)}", flush=True)
dist = Counter(f[:7] for _, f, _ in old_missing)
print("老股票缺历史段的数据起点分布(年-月):", dict(sorted(dist.items())), flush=True)
print("老股票样例(前 12):", [(c, f, ld) for c, f, ld in old_missing[:12]], flush=True)

# --- 37 缺行码明细（缺口日期样本） ---
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]
dates_by_code = (daily.filter(pl.col("code").is_in(code_set))
                 .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())
have_map = {r["code"]: set(r["ds"]) for r in dates_by_code}
detail = []
for c in codes:
    have = have_map.get(c)
    if not have:
        continue
    f, l = min(have), max(have)
    miss = [d for d in cal_dates if f <= d <= l and d not in have]
    if miss:
        detail.append((c, miss))
print(f"\n缺行码数: {len(detail)}", flush=True)
for c, miss in detail:
    runs = []
    s = pv = miss[0]
    for d in miss[1:]:
        idx_p = cal_dates.index(pv); idx_d = cal_dates.index(d)
        if idx_d - idx_p <= 3:
            pv = d
        else:
            runs.append((s, pv)); s = pv = d
    runs.append((s, pv))
    print(f"  {c}: 共 {len(miss)} 天, 段: {runs}", flush=True)

# --- 因子差行明细（抽 3 码看差哪天） ---
adj = store.read_adj_factor(codes, str(ROOT / "data"))
adj_map = {r["code"]: set(r["ds"]) for r in
           adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
checked = 0
for c in codes:
    have = have_map.get(c)
    a = adj_map.get(c)
    if not have or a is None:
        continue
    diff = have - a
    if diff and checked < 5:
        print(f"\n因子缺失日 {c}: {sorted(diff)[:5]}", flush=True)
        checked += 1
