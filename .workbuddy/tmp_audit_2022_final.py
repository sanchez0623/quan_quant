# -*- coding: utf-8 -*-
"""2022 口径最终版：统一判据（首日-上市日 >14 天 = 真缺）重算全部数字"""
import sys, json, pathlib
from collections import Counter
import datetime
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
END = str(daily["date"].max())
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if FIRST_TD <= d <= END]

first_map = {r["code"]: r["first"] for r in
             daily.group_by("code").agg(pl.col("date").min().alias("first")).to_dicts()}
split = json.loads((ROOT / ".workbuddy" / "zz500_late_start_split.json").read_text(encoding="utf-8"))
ipo_map = {c: ipo for c, f, ipo in split["ipo_natural"] + split["old_missing"] + split["unknown"]}

late = {c: f for c, f in first_map.items() if f > FIRST_TD}
natural, real_missing = [], []
for c, f in sorted(late.items()):
    ipo = ipo_map.get(c)
    if ipo:
        lag = (datetime.date.fromisoformat(f) - datetime.date.fromisoformat(ipo)).days
        if lag <= 14:
            natural.append(c)
            continue
    real_missing.append((c, f, ipo))

miss_days = {c: sum(1 for d in cal_dates if d < f) for c, f, _ in real_missing}
total_miss = sum(miss_days.values())
dist = Counter(f[:7] for _, f, _ in real_missing)

print(f"=== 2022 口径最终（统一判据）===", flush=True)
print(f"日K齐(起点<=2022-01-04 或 首日≈上市日): {967 - len(real_missing)}/967", flush=True)
print(f"真缺段: {len(real_missing)} 码 / {total_miss} 交易日", flush=True)
print("  缺段码数据起点分布:", dict(sorted(dist.items())), flush=True)
print("  自然起点(IPO):", len(natural), "码", flush=True)

# 缺行（2022 窗口）
have_map = {r["code"]: set(r["ds"]) for r in
            (daily.filter(pl.col("code").is_in(set(codes)))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
bad_daily = {}
for c in codes:
    have = have_map.get(c)
    if not have:
        continue
    f, l = min(have), max(have)
    miss = [d for d in cal_dates if f <= d <= l and d not in have]
    if miss:
        bad_daily[c] = miss
print(f"日K缺行(在市窗口内): {len(bad_daily)} 码 / {sum(len(v) for v in bad_daily.values())} 天", flush=True)
sep = sum(1 for v in bad_daily.values() for d in v if d >= "2026-09-08")
print(f"  其中 2026-09-08 后(日常更新缺漏): {sep} 天 | 之前散布: {sum(len(v) for v in bad_daily.values()) - sep} 天", flush=True)

# 因子
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
fg = {c: len({d for d in (have_map.get(c, set()) - a_map.get(c, set())) if d >= FIRST_TD})
      for c in codes}
fg = {c: n for c, n in fg.items() if n}
print(f"因子: {len(fg)} 码 / {sum(fg.values())} 天", flush=True)
