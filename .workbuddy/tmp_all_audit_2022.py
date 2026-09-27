# -*- coding: utf-8 -*-
"""最终复验：全库 5261 码 2022 口径三层（日K/因子/5分钟K）
起点合法判定：首日<=2022-01-04 | 首日≈上市日(±14天) | 2022-01-04 后上市（次新，2022前无数据正常）"""
import sys, pathlib, datetime
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource

FIRST_TD = "2022-01-04"
daily_all = store.read_daily(None, str(ROOT / "data"))
END = str(daily_all["date"].max())
all_codes = sorted(daily_all["code"].unique().to_list())
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if FIRST_TD <= d <= END]

# 上市日
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

first_map = {r["code"]: r["first"] for r in
             (daily_all.filter((pl.col("date") >= "2021-01-01"))
              .group_by("code").agg(pl.col("date").min().alias("first")).to_dicts())}
have_map = {r["code"]: set(r["ds"]) for r in
            (daily_all.filter((pl.col("date") >= FIRST_TD))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}

def start_ok(c):
    f = first_map.get(c)
    if not f:
        return False, "无数据"
    if f <= FIRST_TD:
        return True, f
    ipo = ipo_map.get(c)
    if ipo and ipo >= FIRST_TD:
        return True, f + "(次新)"
    if ipo:
        lag = (datetime.date.fromisoformat(f) - datetime.date.fromisoformat(ipo)).days
        if lag <= 14:
            return True, f + "(≈ipo)"
    return False, f

bad_start, bad_rows = [], 0
for c in all_codes:
    ok, note = start_ok(c)
    if not ok:
        bad_start.append((c, note))
    have = have_map.get(c, set())
    if have:
        f, l = min(have), max(have)
        miss = sum(1 for d in cal_dates if f <= d <= l and d not in have)
        bad_rows += miss
print(f"[日K·2022口径] 全库 {len(all_codes)} 码", flush=True)
print(f"  起点不合法(老股票缺段): {len(bad_start)} 码 {bad_start[:15]}", flush=True)
print(f"  在市窗口缺行: {bad_rows} 天", flush=True)

# 因子
adj = store.read_adj_factor(None, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
fg = {c: len({d for d in have_map.get(c, set()) if d >= FIRST_TD} - a_map.get(c, set()))
      for c in all_codes}
fg = {c: n for c, n in fg.items() if n}
print(f"[因子·2022口径] 缺口 {len(fg)} 码 / {sum(fg.values())} 天", flush=True)
if fg:
    print("  ", sorted(fg.items(), key=lambda x: -x[1])[:10], flush=True)

# 5分钟K（2022 起，真实成交日基准）
m5dir = ROOT / "data" / "minute5"
day_map = {c: {d for d in ds if d >= FIRST_TD} for c, ds in
           (daily_all.filter((pl.col("date") >= FIRST_TD) & (pl.col("volume").is_not_null())
                             & (pl.col("volume") > 0))
            .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
OUT = ROOT / ".workbuddy" / "c_m5_audit.jsonl"
OUT.unlink(missing_ok=True)
with OUT.open("a", encoding="utf-8") as f:
    for i, c in enumerate(all_codes):
        dates = day_map.get(c, set())
        p = m5dir / f"{c}.parquet"
        if p.exists():
            try:
                m5 = pl.read_parquet(p, columns=["date"])
                have = set(m5["date"].str.slice(0, 10).unique().to_list())
            except Exception:
                have = set()
        else:
            have = set()
        f.write('{"code":"%s","gap":%s}\n' % (c, sorted(dates - have)))
rows = [__import__("json").loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
gmap = {r["code"]: r["gap"] for r in rows if r["gap"]}
print(f"[5分钟K·2022口径] 缺口 {len(gmap)} 码 / {sum(len(v) for v in gmap.values())} 天", flush=True)
for c, v in sorted(gmap.items()):
    print(f"    {c}: {len(v)} 天", flush=True)

print("\n=== 汇总 ===", flush=True)
print(f"日K: 起点不合法 {len(bad_start)} 码 | 缺行 {bad_rows} 天", flush=True)
print(f"因子: {len(fg)} 码 / {sum(fg.values())} 天", flush=True)
print(f"5分钟K: {len(gmap)} 码 / {sum(len(v) for v in gmap.values())} 天", flush=True)
