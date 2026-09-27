# -*- coding: utf-8 -*-
"""其他 4294 码：起点拆分（IPO自然 vs 老股票真缺段，判据首日-上市日>14天）"""
import sys, json, pathlib
from collections import Counter
import datetime
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource

START = "2021-01-01"
daily_all = store.read_daily(None, str(ROOT / "data"))
all_codes = sorted(daily_all["code"].unique().to_list())
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
zz500 = set(hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
            ["code"].unique().to_list())
others = [c for c in all_codes if c not in zz500]
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= str(daily_all["date"].max())]
first_map = {r["code"]: r["first"] for r in
             (daily_all.filter((pl.col("date") >= START))
              .group_by("code").agg(pl.col("date").min().alias("first")).to_dicts())}

bs = BaostockSource()
def _q():
    rs = bs._bs.query_stock_basic()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q)
ipo_map = {}
if res:
    fields, rows = res
    i_code, i_ipo = fields.index("code"), fields.index("ipoDate")
    for r in rows:
        ipo_map[r[i_code].split(".")[-1]] = r[i_ipo]
print(f"baostock 基础信息: {len(ipo_map)} 只", flush=True)

natural, missing = [], []
for c in others:
    f = first_map.get(c)
    ipo = ipo_map.get(c)
    if not f:
        continue
    if ipo:
        lag = (datetime.date.fromisoformat(f) - datetime.date.fromisoformat(ipo)).days
        if lag <= 14:
            natural.append(c)
            continue
    missing.append((c, f, ipo))

# 真缺段量
td_index = {d: i for i, d in enumerate(cal_dates)}
total_days = 0
by_start = Counter()
for c, f, ipo in missing:
    n = sum(1 for d in cal_dates if d < f)
    total_days += n
    by_start[f[:7]] += 1
print(f"\n[其他·起点拆分] IPO自然 {len(natural)} | 真缺段 {len(missing)} 码", flush=True)
print("真缺段码的数据起点分布(年-月, 前12):", dict(sorted(by_start.items(), key=lambda x: -x[1])[:12]), flush=True)
print(f"真缺段合计: {total_days} 交易日（对比 zz500 缺段 112,695）", flush=True)

# 缺行码数（排除缺段码，单独算）
have_map = {r["code"]: set(r["ds"]) for r in
            (daily_all.filter((pl.col("date") >= START))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
miss_set = {c for c, _, _ in missing}
rows_gap, seg_gap = 0, 0
n_rows_codes, n_seg_codes = 0, 0
for c in others:
    have = have_map.get(c)
    if not have:
        continue
    f, l = min(have), max(have)
    if c in miss_set:
        seg_gap += sum(1 for d in cal_dates if d < f)
        n_seg_codes += 1
    n = sum(1 for d in cal_dates if f <= d <= l and d not in have)
    if n:
        rows_gap += n
        n_rows_codes += 1
print(f"\n缺行(不含缺段): {n_rows_codes} 码 / {rows_gap} 天", flush=True)
print(f"缺段: {n_seg_codes} 码 / {seg_gap} 天", flush=True)

(WORK := ROOT / ".workbuddy" / "others_missing_split.json").write_text(
    json.dumps({"missing": missing, "natural": natural}, ensure_ascii=False), encoding="utf-8")
print("拆分结果已存 others_missing_split.json", flush=True)
