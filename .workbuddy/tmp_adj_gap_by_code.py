# -*- coding: utf-8 -*-
"""按码聚合因子缺口：缺多少码、每码缺多少天、缺口段概要"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= "2021-01-01"))
codes = sorted(seg["code"].unique().to_list())

daily = store.read_daily(codes, str(ROOT / "data"))
d_map = {r["code"]: set(r["ds"]) for r in
         daily.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}
adj = store.read_adj_factor(codes, str(ROOT / "data"))
a_map = {r["code"]: set(r["ds"]) for r in
         adj.group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts()}

only_latest, real_gap = [], []
for c, have in d_map.items():
    a = a_map.get(c, set())
    diff = sorted(have - a)
    if not diff:
        continue
    if diff == ["2026-09-07"]:
        only_latest.append(c)
    else:
        # 合并缺口段
        cal_sorted = diff
        runs, s, p = [], cal_sorted[0], cal_sorted[0]
        for d in cal_sorted[1:]:
            runs.append(None) if False else None
            # 用日期序号判断相邻（这里直接用日历间隔<=5自然日近似）
            import datetime
            dd = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(p)).days
            if dd <= 7:
                p = d
            else:
                runs.append((s, p)); s = p = d
        runs.append((s, p))
        real_gap.append((c, len(diff), runs[:5]))

print(f"因子有缺口码数: {len(only_latest) + len(real_gap)}", flush=True)
print(f"  仅差最新日 2026-09-07: {len(only_latest)} 码（日常更新会自动补）", flush=True)
print(f"  真实缺口: {len(real_gap)} 码", flush=True)
total = sum(n for _, n, _ in real_gap)
print(f"  真实缺口合计 {total} 天", flush=True)
for c, n, runs in sorted(real_gap, key=lambda x: -x[1])[:30]:
    print(f"  {c}: {n} 天 段:{runs}", flush=True)
