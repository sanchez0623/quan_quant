# -*- coding: utf-8 -*-
"""精确缺口扫描：全库 2022-01-04~最新，日线真实成交日(vol>0) vs 分钟已有日期
正确 JSON 写法（json.dumps）。逐码 append jsonl 防崩。"""
import sys, json, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

FIRST_TD = "2022-01-04"
M5DIR = ROOT / "data" / "minute5"
daily_all = store.read_daily(None, str(ROOT / "data"))
END = str(daily_all["date"].max())
all_codes = sorted(daily_all["code"].unique().to_list())
day_map = {r["code"]: set(r["ds"]) for r in
           (daily_all.filter((pl.col("date") >= FIRST_TD) & (pl.col("volume").is_not_null())
                             & (pl.col("volume") > 0))
            .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}

OUT = ROOT / ".workbuddy" / "c_m5_gap_final.jsonl"
OUT.unlink(missing_ok=True)
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
            print(f"  扫描 {i + 1}/{len(all_codes)}", flush=True)

rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
gmap = {r["code"]: r["gap"] for r in rows if r["gap"]}
total = sum(len(v) for v in gmap.values())
print(f"\n[精确结果] 分钟缺口 {len(gmap)} 码 / {total} 交易日（基准: 日线真实成交日, {FIRST_TD}~{END}）", flush=True)
# 按缺口段聚合（连续<=3日合并）为拉取计划
plan = {}
for c, gap in gmap.items():
    runs, s, p = [], gap[0], gap[0]
    for d in gap[1:]:
        idx_p = gap.index(p)
        idx_d = gap.index(d)
        if idx_d - idx_p <= 3:
            p = d
        else:
            runs.append([s, p])
            s = p = d
    runs.append([s, p])
    plan[c] = runs
n_seg = sum(len(v) for v in plan.values())
print(f"合并后拉取段: {n_seg} 段", flush=True)
(ROOT / ".workbuddy" / "c_m5_fix_plan.json").write_text(
    json.dumps(plan, ensure_ascii=False), encoding="utf-8")
print("拉取计划已存 c_m5_fix_plan.json", flush=True)
