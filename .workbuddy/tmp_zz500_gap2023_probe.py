# -*- coding: utf-8 -*-
"""探查：691 个 zz500 2021-2023 历史成分码在 2023-03-28 之后的日K缺口"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500")
                  & (pl.col("snap_date") >= "2021-01-01")
                  & (pl.col("snap_date") <= "2023-03-31"))
codes = sorted(seg["code"].unique().to_list())
print(f"历史成分码数: {len(codes)}", flush=True)

daily = store.read_daily(None, str(ROOT / "data"))
lib_max = str(daily["date"].max())
lib_min = str(daily["date"].min())
print(f"库内日K: {lib_min} ~ {lib_max}, 码数 {daily['code'].n_unique()}", flush=True)

cal = store.read_calendar(str(ROOT / "data"))
if cal is not None:
    print(f"交易日历: {cal['date'].min()} ~ {cal['date'].max()}", flush=True)

basic = store.read_stock_basic(str(ROOT / "data"))
delisted = set(basic.filter(pl.col("delisted"))["code"].to_list()) if "delisted" in basic.columns else set()

sub = daily.filter(pl.col("code").is_in(codes))
after = (sub.filter(pl.col("date") > "2023-03-27").group_by("code")
         .agg(pl.col("date").min().alias("next_after")))
na_map = dict(zip(after["code"].to_list(), after["next_after"].to_list()))

gap_alive, gap_dead_suspect, ok = [], [], 0
for c in codes:
    na = na_map.get(c)
    if na is None:
        if c in delisted:
            gap_dead_suspect.append(c)   # 退市且 2023-03-27 后无数据：可能 8-31 清理删掉后又没被 9-04 补回
        else:
            gap_alive.append((c, lib_max))   # 在市但 2023-03-27 后完全无数据 → 缺到 lib_max
    elif str(na) > "2023-04-05":
        gap_alive.append((c, str(na)))       # 2023-03-28 起断档到 next_after
    else:
        ok += 1

print(f"\n连续无缺口: {ok} | 有缺口(在市/有后数据): {len(gap_alive)} | 退市且后无数据(待判断): {len(gap_dead_suspect)}", flush=True)
dist = Counter(g[1][:7] for g in gap_alive)
print("缺口右端点分布(年-月):", dict(sorted(dist.items())), flush=True)
if gap_dead_suspect:
    print("退市且 2023-03-27 后无数据的码:", gap_dead_suspect, flush=True)

need = sorted(c for c, _ in gap_alive) + gap_dead_suspect
(ROOT / ".workbuddy" / "zz500_gap2023_need.txt").write_text("\n".join(need), encoding="utf-8")
print(f"\n需补码清单已存 zz500_gap2023_need.txt ({len(need)} 码)", flush=True)
