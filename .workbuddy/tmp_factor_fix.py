# -*- coding: utf-8 -*-
"""Phase B：因子缺口修复（29 码全历史重拉 + 展开合并）。
铁律：循环内只拉取与内存展开，最后一次性合并单次写库（严禁循环内反复读写全表）。"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource
from app.data.updater import _expand_adj_to_daily

FIRST_TD = "2022-01-04"
hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
have_map = {r["code"]: sorted(set(r["ds"])) for r in
            (daily.filter(pl.col("code").is_in(set(codes)))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

gap_codes = []
for c in codes:
    have = have_map.get(c, [])
    diff = {d for d in have if d >= FIRST_TD} - a_map.get(c, set())
    if diff:
        gap_codes.append(c)
print(f"因子缺口码: {len(gap_codes)}: {gap_codes}", flush=True)

bs = BaostockSource()
frames, ok, fail = [], 0, []
for i, c in enumerate(gap_codes):
    ev = bs.get_adj_factor(c, start="1990-01-01")   # 全历史事件
    if ev is None or ev.height == 0:
        fail.append(c)
        print(f"  [{i + 1}/{len(gap_codes)}] {c}: 因子拉取失败", flush=True)
        continue
    grid = {c: have_map.get(c, [])}
    daily_fac = _expand_adj_to_daily(ev, grid)
    frames.append(daily_fac)
    ok += 1
    print(f"  [{i + 1}/{len(gap_codes)}] {c}: {ev.height} 事件 -> {daily_fac.height} 日行", flush=True)

print(f"\n拉取完成 ok={ok} fail={fail}", flush=True)
if frames:
    new = pl.concat(frames, how="diagonal_relaxed").select(
        ["code", "date", pl.col("adj_factor").cast(pl.Float64)])
    existing = store.read_adj_factor(None, str(ROOT / "data"))
    merged = (pl.concat([existing, new]).unique(subset=["code", "date"], keep="last")
              .sort(["code", "date"]))
    store.write_adj_factor(merged, str(ROOT / "data"))
    print(f"合并写库: {merged.height} 行 / {merged['code'].n_unique()} 码", flush=True)

# 验证
adj2 = store.read_adj_factor(gap_codes, str(ROOT / "data"))
a2_map = {r["code"]: set(r["ds"]) for r in
          adj2.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
left = 0
for c in gap_codes:
    diff = {d for d in have_map.get(c, []) if d >= FIRST_TD} - a2_map.get(c, set())
    if diff:
        left += 1
        print(f"  仍缺 {c}: {len(diff)} 天", flush=True)
print(f"=== Phase B DONE: 剩余缺口码 {left} ===", flush=True)
