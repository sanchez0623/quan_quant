# -*- coding: utf-8 -*-
"""探查 v2（断点续传）：zz500 全部历史成分码 5分钟线缺口。
逐码追加写 jsonl（{code, runs}），崩溃重跑自动跳过已完成码。
基准 = 该码日线日期；已有 = minute5 文件日期。
"""
import sys, json, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

START, END = "2021-01-01", "2026-09-07"
WORK = ROOT / ".workbuddy"
OUT = WORK / "zz500_m5_gap_runs.jsonl"
M5DIR = ROOT / "data" / "minute5"

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= START))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
day_map = (daily.filter((pl.col("date") >= START) & (pl.col("date") <= END)
                        & pl.col("volume").is_not_null() & (pl.col("volume") > 0))
           .group_by("code").agg(pl.col("date").sort().alias("dates"))
           .to_dicts())
day_map = {r["code"]: r["dates"] for r in day_map}

done = set()
if OUT.exists():
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(json.loads(line)["code"])
todo = [c for c in codes if c not in done]
print(f"码数 {len(codes)}，已完成 {len(done)}，待算 {len(todo)}", flush=True)

with OUT.open("a", encoding="utf-8") as f:
    for i, c in enumerate(todo):
        dates = day_map.get(c, [])
        p = M5DIR / f"{c}.parquet"
        if p.exists():
            try:
                m5 = pl.read_parquet(p, columns=["date"])
                have = set(m5["date"].str.slice(0, 10).unique().to_list())
            except Exception:
                have = set()
        else:
            have = set()
        gap = [d for d in dates if d not in have]
        runs = []
        if gap:
            s = pv = gap[0]
            prev_idx = 0
            for d in gap[1:]:
                idx = dates.index(d)
                if idx - prev_idx <= 5:
                    pv = d
                    prev_idx = idx
                else:
                    runs.append([s, pv])
                    s = pv = d
                    prev_idx = idx
            runs.append([s, pv])
        f.write(json.dumps({"code": c, "runs": runs, "gap_days": len(gap)},
                           ensure_ascii=False) + "\n")
        if (i + 1) % 100 == 0:
            f.flush()
            print(f"  进度 {i + 1}/{len(todo)}", flush=True)
    f.flush()

rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
rows = [r for r in rows if r["code"] in set(codes)]
plan = {r["code"]: r["runs"] for r in rows if r["runs"]}
total_gap = sum(r["gap_days"] for r in rows)
seg_counts = [len(r["runs"]) for r in rows if r["runs"]]
print(f"\n有缺口码数: {len(plan)} | 总缺口交易日数: {total_gap}", flush=True)
print("每码段数分布:", dict(sorted(Counter(seg_counts).items())), flush=True)
print("缺口段起年分布:", dict(sorted(Counter(s[:4] for runs in plan.values() for s, _ in runs).items())), flush=True)
print("缺口段终年分布:", dict(sorted(Counter(e[:4] for runs in plan.values() for _, e in runs).items())), flush=True)

(WORK / "zz500_m5_need.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
print(f"拉取计划已存 zz500_m5_need.json（{len(plan)} 码 / {sum(seg_counts)} 段）", flush=True)
