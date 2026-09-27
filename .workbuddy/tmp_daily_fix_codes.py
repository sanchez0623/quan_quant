# -*- coding: utf-8 -*-
"""生成 2022 口径日K补拉码清单：186 缺段码 ∪ 351 缺行码"""
import sys, json, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

FIRST_TD = "2022-01-04"
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())
code_set = set(codes)

daily = store.read_daily(codes, str(ROOT / "data"))
END = str(daily["date"].max())
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if FIRST_TD <= d <= END]

first_map = {r["code"]: r["first"] for r in
             daily.group_by("code").agg(pl.col("date").min().alias("first")).to_dicts()}
split = json.loads((ROOT / ".workbuddy" / "zz500_late_start_split.json").read_text(encoding="utf-8"))
ipo_map = {c: ipo for c, f, ipo in split["ipo_natural"] + split["old_missing"] + split["unknown"]}

import datetime
miss_seg_codes, miss_row_codes = set(), set()
for c in codes:
    f = first_map.get(c)
    ipo = ipo_map.get(c)
    if f and f > FIRST_TD and (not ipo or (datetime.date.fromisoformat(f)
                                           - datetime.date.fromisoformat(ipo)).days > 14):
        miss_seg_codes.add(c)          # 缺历史段
have_map = {r["code"]: set(r["ds"]) for r in
            (daily.filter(pl.col("code").is_in(code_set))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
for c in codes:
    have = have_map.get(c)
    if not have:
        miss_row_codes.add(c)
        continue
    f, l = min(have), max(have)
    if any(d not in have for d in cal_dates if f <= d <= l):
        miss_row_codes.add(c)          # 有缺行

need = sorted(miss_seg_codes | miss_row_codes)
(ROOT / ".workbuddy" / "zz500_daily_fix_codes.txt").write_text("\n".join(need), encoding="utf-8")
print(f"缺段 {len(miss_seg_codes)} | 缺行 {len(miss_row_codes)} | 并集 {len(need)} 码", flush=True)
print(f"区间: {FIRST_TD} ~ {END}", flush=True)
print("清单已存 zz500_daily_fix_codes.txt", flush=True)
