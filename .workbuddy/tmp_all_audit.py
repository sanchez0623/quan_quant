# -*- coding: utf-8 -*-
"""全库体检（5,261 码）：日线/因子/5分钟K，窗口 2021-01-01~最新。
输出：起点分布、缺行、因子缺口、分钟缺口；并区分 zz500(967) 与其他(4294)。"""
import sys, json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

WORK = ROOT / ".workbuddy"
M5DIR = ROOT / "data" / "minute5"
START = "2021-01-01"

daily_all = store.read_daily(None, str(ROOT / "data"))
END = str(daily_all["date"].max())
all_codes = sorted(daily_all["code"].unique().to_list())

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
zz500 = set(hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
            ["code"].unique().to_list())
others = [c for c in all_codes if c not in zz500]
print(f"全库 {len(all_codes)} 码, {START}~{END} | zz500 {len(zz500)} | 其他 {len(others)}", flush=True)

cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]

have_map = {r["code"]: set(r["ds"]) for r in
            (daily_all.filter((pl.col("date") >= START) & (pl.col("date") <= END))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
first_map = {c: min(v) for c, v in have_map.items() if v}

def summarize(code_list, tag):
    n_have = sum(1 for c in code_list if have_map.get(c))
    starts = Counter((first_map[c][:7] if c in first_map else "无数据")
                     for c in code_list)
    print(f"\n[{tag}] {len(code_list)} 码, 有日K {n_have}", flush=True)
    print("  起点分布(年):", dict(sorted(Counter(k[:4] for k in starts).items())), flush=True)
    bad, fdays = 0, 0
    for c in code_list:
        have = have_map.get(c)
        if not have:
            continue
        f, l = min(have), max(have)
        miss = sum(1 for d in cal_dates if f <= d <= l and d not in have)
        if miss:
            bad += 1
            fdays += miss
    print(f"  缺行: {bad} 码 / {fdays} 天", flush=True)
    return bad, fdays

summarize(all_codes, "全库")
summarize(zz500, "zz500(967)")
summarize(others, "其他")

# 因子缺口（全库，2021 窗口）
adj = store.read_adj_factor(None, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
for tag, code_list in (("全库", all_codes), ("其他", others)):
    fg = {}
    for c in code_list:
        have = have_map.get(c, set())
        diff = {d for d in have - a_map.get(c, set()) if d >= START}
        if diff:
            fg[c] = len(diff)
    print(f"[因子·{tag}] 缺口 {len(fg)} 码 / {sum(fg.values())} 天", flush=True)

# 5分钟K（全库，逐码 append jsonl 防崩）
OUT = WORK / "all_m5_audit.jsonl"
OUT.unlink(missing_ok=True)
day_map = {c: {d for d in ds if d >= START} for c, ds in
           (daily_all.filter((pl.col("date") >= START) & (pl.col("volume").is_not_null())
                             & (pl.col("volume") > 0))
            .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
with OUT.open("a", encoding="utf-8") as f:
    for i, c in enumerate(all_codes):
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
        if (i + 1) % 500 == 0:
            f.flush()
            print(f"  分钟扫描 {i + 1}/{len(all_codes)}", flush=True)

rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
gmap = {r["code"]: r["gap"] for r in rows if r["gap"]}
for tag, code_list in (("全库", all_codes), ("其他", others)):
    sub = {c: v for c, v in gmap.items() if c in set(code_list)}
    print(f"[5分钟K·{tag}] 缺口 {len(sub)} 码 / {sum(len(v) for v in sub.values())} 天", flush=True)
dist = Counter(d[:7] for v in gmap.values() for d in v)
print("  全库缺口日分布(年):", dict(sorted(Counter(k[:4] for k in dist).items())), flush=True)
