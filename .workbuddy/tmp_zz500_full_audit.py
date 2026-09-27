# -*- coding: utf-8 -*-
"""最终全量体检：967 个 zz500 历史成分码，日线/因子/5分钟K 至最新日期
- 日K起点分类（复用 late_start_split）+ 在市窗口缺行
- 因子缺口按码聚合
- 5分钟K：真实成交日(vol>0)基准 vs 分钟文件，逐码 append jsonl 防崩
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

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())
code_set = set(codes)

daily = store.read_daily(codes, str(ROOT / "data"))
END = str(daily["date"].max())
START = "2021-01-01"
print(f"口径: 967 码, {START} ~ {END}", flush=True)

cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]

# ---------- 日K：起点 + 缺行 ----------
g = (daily.group_by("code").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"),
                                pl.col("date").n_unique().alias("nd"))
     .filter(pl.col("code").is_in(code_set)))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list()))
early = [c for c in codes if first_map.get(c, "9999") <= "2021-01-04"]
late = {c: f for c, f in first_map.items() if f > "2021-01-04"}

split = json.loads((WORK / "zz500_late_start_split.json").read_text(encoding="utf-8"))
ipo_n = len(split["ipo_natural"]); old_n = len(split["old_missing"])

have_map = {r["code"]: set(r["ds"]) for r in
            (daily.filter(pl.col("code").is_in(code_set))
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
print(f"\n[日K] 起点<=2021-01-04: {len(early)} | 晚起点: {len(late)} (=IPO自然 {ipo_n} + 老股票缺段 {old_n})", flush=True)
print(f"[日K] 在市窗口缺行: {len(bad_daily)} 码 / 共 {sum(len(v) for v in bad_daily.values())} 天", flush=True)

# ---------- 因子缺口 ----------
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
factor_gap = {}
for c in codes:
    have = have_map.get(c, set())
    a = a_map.get(c, set())
    diff = have - a
    if diff:
        factor_gap[c] = len(diff)
print(f"[因子] 有缺口: {len(factor_gap)} 码 / 共 {sum(factor_gap.values())} 天", flush=True)

# ---------- 5分钟K（真实成交日基准，逐码 append jsonl） ----------
OUT = WORK / "zz500_m5_audit.jsonl"
OUT.unlink(missing_ok=True)
day_map = (daily.filter((pl.col("date") >= START) & (pl.col("date") <= END)
                        & pl.col("volume").is_not_null() & (pl.col("volume") > 0))
           .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())
day_map = {r["code"]: set(r["ds"]) for r in day_map}

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
        gap = dates - have
        f.write(json.dumps({"code": c, "gap": sorted(gap)}, ensure_ascii=False) + "\n")
        if (i + 1) % 300 == 0:
            f.flush()
            print(f"  扫描 {i + 1}/{len(codes)}", flush=True)

rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
m5_gap = {r["code"]: r["gap"] for r in rows if r["gap"]}
total_m5 = sum(len(v) for v in m5_gap.values())
print(f"\n[5分钟K] 真实成交日缺口: {len(m5_gap)} 码 / 共 {total_m5} 天", flush=True)
dist = Counter(g[:7] for v in m5_gap.values() for g in v)
print("  缺口日分布(年-月, 前15):", dict(sorted(dist.items())[:15]), flush=True)
# 已知源端缺 vs 新缺口
known = {"002920", "688065", "302132"}
new_codes = [c for c in m5_gap if c not in known]
print(f"  已知源端缺: {sorted(set(m5_gap) & known)} | 其余: {len(new_codes)} 码", flush=True)

print("\n=== 体检汇总 ===", flush=True)
print(f"日K: {len(early)} 码从 2021-01-04 起；晚起点 {len(late)}（IPO {ipo_n} + 老股票缺段 {old_n}，缺 112,695 交易日）；缺行 {len(bad_daily)} 码", flush=True)
print(f"因子: 缺 {len(factor_gap)} 码 / {sum(factor_gap.values())} 天", flush=True)
print(f"5分钟K: 缺 {len(m5_gap)} 码 / {total_m5} 天", flush=True)
