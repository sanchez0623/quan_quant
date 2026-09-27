# -*- coding: utf-8 -*-
"""体检（2022-01-01 口径）：967 个 zz500 历史成分码，日线/因子/5分钟K 至最新
- 日K起点：<=2022-01-04 齐；晚起点再分 IPO 自然 vs 老股票缺段
- 日K缺行、因子缺口、5分钟K缺口（真实成交日基准）+ 老股票缺段码的隐藏分钟缺口
"""
import sys, json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

WORK = ROOT / ".workbuddy"
M5DIR = ROOT / "data" / "minute5"
NEW_START = "2022-01-01"
FIRST_TD = "2022-01-04"   # 2022 年首个交易日（日历取实际）

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
END = str(daily["date"].max())
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if NEW_START <= d <= END]
# 实际 2022 首个交易日
first_td = next(d for d in cal_dates if d >= NEW_START)
print(f"口径: 967 码, {NEW_START}({first_td}) ~ {END}", flush=True)

# ---------- 1) 日K起点 ----------
g = (daily.group_by("code").agg(pl.col("date").min().alias("first"))
     .filter(pl.col("code").is_in(set(codes))))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list()))
late = {c: f for c, f in first_map.items() if f > first_td}

split = json.loads((WORK / "zz500_late_start_split.json").read_text(encoding="utf-8"))
ipo_map = {c: ipo for c, f, ipo in split["ipo_natural"] + split["old_missing"] + split["unknown"]}
ipo_natural, old_missing = [], []
for c, f in sorted(late.items()):
    ipo = ipo_map.get(c)
    if ipo and ipo > first_td:
        ipo_natural.append(c)
    else:
        old_missing.append((c, f, ipo))
print(f"\n[日K起点] <= {first_td}: {len(codes) - len(late)} 码 | 晚起点 {len(late)} "
      f"= IPO自然 {len(ipo_natural)} + 老股票缺段 {len(old_missing)}", flush=True)
dist = Counter(f[:7] for _, f, _ in old_missing)
print("  老股票缺段码的数据起点分布:", dict(sorted(dist.items())), flush=True)

# 老股票缺段量
total_miss = sum(sum(1 for d in cal_dates if d < f) for _, f, _ in old_missing)
print(f"  老股票缺历史段合计: {total_miss} 交易日", flush=True)

# ---------- 2) 日K缺行 ----------
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
print(f"\n[日K缺行] {len(bad_daily)} 码 / {sum(len(v) for v in bad_daily.values())} 天", flush=True)

# ---------- 3) 因子 ----------
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
factor_gap = {}
for c in codes:
    have = have_map.get(c, set())
    diff = have - a_map.get(c, set())
    if diff:
        factor_gap[c] = len(diff)
print(f"[因子] 缺口 {len(factor_gap)} 码 / {sum(factor_gap.values())} 天", flush=True)

# ---------- 4) 5分钟K ----------
# a) 已覆盖段（日线真实成交日基准，2022 起窗口）
OUT = WORK / "zz500_m5_audit_2022.jsonl"
OUT.unlink(missing_ok=True)
day_map = {c: {d for d in ds if d >= first_td} for c, ds in
           (daily.filter((pl.col("date") >= first_td) & (pl.col("volume").is_not_null())
                         & (pl.col("volume") > 0))
            .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
with OUT.open("a", encoding="utf-8") as f:
    for i, c in enumerate(codes):
        dates = day_map.get(c, set())
        p = M5DIR / f"{c}.parquet"
        if p.exists():
            try:
                m5 = pl.read_parquet(p, columns=["date"])
                have = set(m5["date"].str.slice(0, 10).unique().to_list())
            except Exception:
                have = set()
        else:
            have = set()
        f.write(json.dumps({"code": c, "gap": sorted(dates - have)}, ensure_ascii=False) + "\n")
rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
m5_gap = {r["code"]: r["gap"] for r in rows if r["gap"]}
print(f"\n[5分钟K·已覆盖段] 缺口 {len(m5_gap)} 码 / {sum(len(v) for v in m5_gap.values())} 天", flush=True)
for c, v in sorted(m5_gap.items()):
    print(f"    {c}: {len(v)} 天 ({v[0]}~{v[-1]})", flush=True)

# b) 老股票缺段码的隐藏分钟缺口（日线没有→基准看不到；用日历交易日估上限）
hidden = 0
for c, f, _ in old_missing:
    hidden += sum(1 for d in cal_dates if d < f)
print(f"[5分钟K·隐藏缺口] 老股票缺段码 2022-01-04~各自日线首日 的日历交易日上限: {hidden} 天"
      f"（与日K缺段同段，补日K时需同步补分钟）", flush=True)

print("\n=== 2022 口径汇总 ===", flush=True)
print(f"日K: {len(codes) - len(late)}/967 从 {first_td} 起；晚起点 {len(late)}（IPO {len(ipo_natural)} + "
      f"老股票缺段 {len(old_missing)} 码/{total_miss} 交易日）；缺行 {len(bad_daily)} 码/"
      f"{sum(len(v) for v in bad_daily.values())} 天", flush=True)
print(f"因子: {len(factor_gap)} 码 / {sum(factor_gap.values())} 天", flush=True)
print(f"5分钟K: 已覆盖段缺 {len(m5_gap)} 码/{sum(len(v) for v in m5_gap.values())} 天；"
      f"隐藏缺口(与日K同段) {hidden} 交易日上限", flush=True)
