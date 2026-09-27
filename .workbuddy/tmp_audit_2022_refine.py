# -*- coding: utf-8 -*-
"""2022 口径精确化：因子缺口窗口重算 + IPO 自然码首日/上市日校验"""
import sys, json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

FIRST_TD = "2022-01-04"
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if FIRST_TD <= d <= str(daily["date"].max())]

have_map = {r["code"]: set(r["ds"]) for r in
            (daily.filter(pl.col("code").is_in(set(codes)))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

# 因子缺口（2022 窗口）
factor_gap = {}
for c in codes:
    have = have_map.get(c, set())
    diff = {d for d in (have - a_map.get(c, set())) if d >= FIRST_TD}
    if diff:
        factor_gap[c] = len(diff)
print(f"[因子·2022窗口] 缺口 {len(factor_gap)} 码 / {sum(factor_gap.values())} 天", flush=True)
for c, n in sorted(factor_gap.items(), key=lambda x: -x[1])[:8]:
    print(f"    {c}: {n} 天", flush=True)

# IPO 自然码校验：首日 vs 上市日
split = json.loads((ROOT / ".workbuddy" / "zz500_late_start_split.json").read_text(encoding="utf-8"))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list())) if False else {
    r["code"]: r["first"] for r in
    daily.group_by("code").agg(pl.col("date").min().alias("first")).to_dicts()}
import datetime
suspect = []
for c, f, ipo in split["ipo_natural"]:
    fd = first_map.get(c)
    if fd and ipo:
        lag = (datetime.date.fromisoformat(fd) - datetime.date.fromisoformat(ipo)).days
        if lag > 14:
            suspect.append((c, ipo, fd, lag))
print(f"\n[IPO自然码校验] 102 码中首日晚于上市日>14天的: {len(suspect)} 码", flush=True)
for c, ipo, fd, lag in suspect:
    print(f"    {c}: ipo={ipo} 首日={fd} 差{lag}天 ← 真缺段", flush=True)
