# -*- coding: utf-8 -*-
"""精确拆分：269 个晚起点码 = IPO自然起点 vs 老股票缺历史段（baostock ipoDate）"""
import sys, json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource

START = "2021-01-01"
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= START))
codes = sorted(seg["code"].unique().to_list())
code_set = set(codes)
snap691 = set(hist.filter((pl.col("index_key") == "zz500")
                          & (pl.col("snap_date") >= "2021-01-01")
                          & (pl.col("snap_date") <= "2023-03-31"))["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
g = (daily.group_by("code").agg(pl.col("date").min().alias("first"))
     .filter(pl.col("code").is_in(code_set)))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list()))
late = {c: f for c, f in first_map.items() if f > "2021-01-04"}
print(f"晚起点码: {len(late)}", flush=True)

# baostock 全量基础信息（一次调用，拿 ipoDate）
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
    i_code = fields.index("code")
    i_ipo = fields.index("ipoDate")
    for r in rows:
        code = r[i_code].split(".")[-1]
        ipo_map[code] = r[i_ipo]
    print(f"baostock 全量基础信息: {len(rows)} 只", flush=True)
else:
    print("baostock 查询失败", flush=True)

ipo_natural, old_missing, unknown = [], [], []
for c, f in sorted(late.items()):
    ipo = ipo_map.get(c)
    if ipo and ipo > "2021-01-04":
        ipo_natural.append((c, f, ipo))
    elif ipo and ipo <= "2021-01-04":
        old_missing.append((c, f, ipo))
    else:
        unknown.append((c, f, ipo))

print(f"\n[IPO自然起点] {len(ipo_natural)} 码（2021-01-04 后上市，首日=上市日附近）", flush=True)
print(f"[老股票真缺段] {len(old_missing)} 码（2021-01-04 前已上市，但数据起点晚）", flush=True)
dist = Counter(f[:7] for _, f, _ in old_missing)
print("  老股票数据起点分布(年-月):", dict(sorted(dist.items())), flush=True)
print("  样例:", old_missing[:10], flush=True)
if unknown:
    print(f"[无法判断(baostock无记录)] {len(unknown)} 码:", unknown[:20], flush=True)

# 老股票缺段量：2021-01-04 ~ 首日前一交易日 的交易日数
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if d >= "2021-01-04"]
total_miss_days = 0
per_code = {}
for c, f, _ in old_missing:
    n = sum(1 for d in cal_dates if d < f)
    per_code[c] = n
    total_miss_days += n
print(f"\n老股票缺历史段合计交易日数: {total_miss_days}（日均 {total_miss_days // max(len(old_missing),1)} 天/码）", flush=True)
print("缺段最多前 10:", sorted(per_code.items(), key=lambda x: -x[1])[:10], flush=True)

(WORK := ROOT / ".workbuddy" / "zz500_late_start_split.json").write_text(
    json.dumps({"ipo_natural": ipo_natural, "old_missing": old_missing,
                "unknown": unknown, "miss_days": per_code}, ensure_ascii=False),
    encoding="utf-8")
print("分类结果已存 zz500_late_start_split.json", flush=True)
