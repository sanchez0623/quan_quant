# -*- coding: utf-8 -*-
"""B2：全库因子缺口修复（144 码全历史重拉 + 展开合并，一次性写库）。"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource
from app.data.updater import _expand_adj_to_daily

START = "2021-01-01"
DATA = str(ROOT / "data")
daily_all = store.read_daily(None, DATA)
have_map = {r["code"]: sorted(set(r["ds"])) for r in
            (daily_all.filter((pl.col("date") >= START))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
adj = store.read_adj_factor(None, DATA)
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

gap_codes = sorted(c for c, have in have_map.items()
                   if {d for d in have if d >= START} - a_map.get(c, set()))
print(f"因子缺口码: {len(gap_codes)}", flush=True)
print(gap_codes, flush=True)

bs = BaostockSource()
frames, fail = [], []
for i, c in enumerate(gap_codes):
    ev = None
    for attempt in range(2):
        try:
            ev = bs.get_adj_factor(c, start="1990-01-01")
            break
        except Exception as ex:  # noqa: BLE001
            print(f"  {c} 异常{attempt + 1}: {type(ex).__name__}", flush=True)
            try:
                bs._force_logout(); bs._ensure_login()
            except Exception:
                pass
    if ev is None or ev.height == 0:
        fail.append(c)
        print(f"  [{i + 1}/{len(gap_codes)}] {c}: 拉取失败", flush=True)
        continue
    daily_fac = _expand_adj_to_daily(ev, {c: have_map.get(c, [])})
    frames.append(daily_fac)
    print(f"  [{i + 1}/{len(gap_codes)}] {c}: {ev.height} 事件 -> {daily_fac.height} 日行", flush=True)

print(f"\n拉取完成 ok={len(frames)} fail={fail}", flush=True)
if frames:
    new = pl.concat(frames, how="diagonal_relaxed").select(
        ["code", "date", pl.col("adj_factor").cast(pl.Float64)])
    existing = store.read_adj_factor(None, DATA)
    merged = (pl.concat([existing, new]).unique(subset=["code", "date"], keep="last")
              .sort(["code", "date"]))
    store.write_adj_factor(merged, DATA)
    print(f"合并写库: {merged.height} 行 / {merged['code'].n_unique()} 码", flush=True)

# 复验
adj2 = store.read_adj_factor(None, DATA)
a2_map = {r["code"]: set(r["ds"]) for r in
          adj2.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
left = {c: len({d for d in have_map.get(c, []) if d >= START} - a2_map.get(c, set()))
        for c in gap_codes}
left = {c: n for c, n in left.items() if n}
print(f"=== B2 DONE: 剩余缺口 {len(left)} 码 {left} ===", flush=True)
