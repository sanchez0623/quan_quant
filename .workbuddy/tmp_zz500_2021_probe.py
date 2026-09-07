# -*- coding: utf-8 -*-
"""探查：中证500 在 2021-01~2023-03-27 的历史成分，及日K覆盖缺口"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")

# zz500 在 2021-01-01 ~ 2023-03-27 快照中的成分
seg = hist.filter((pl.col("index_key") == "zz500")
                  & (pl.col("snap_date") >= "2021-01-01")
                  & (pl.col("snap_date") <= "2023-03-31"))
codes = sorted(seg["code"].unique().to_list())
print(f"zz500 2021-01~2023-03-27 快照成分码数: {len(codes)}", flush=True)
print(f"快照次数: {seg['snap_date'].n_unique()} 次", flush=True)
print("前缀分布:", dict(Counter(c[:3] for c in codes)), flush=True)

daily = store.read_daily(None, str(ROOT / "data"))
db_all = set(daily["code"].unique().to_list())
have = [c for c in codes if c in db_all]
no_db = [c for c in codes if c not in db_all]
print(f"\n库内已有日K: {len(have)} | 完全不在库(无任何日K): {len(no_db)}", flush=True)
if no_db:
    print("  不在库码:", no_db, flush=True)

# 有日K码的起点检查：是否覆盖 2021-01-01
START = "2021-01-01"
# 先看 daily 中这些码的最早日期
first = daily.filter(pl.col("code").is_in(have)).group_by("code").agg(
    pl.col("date").min().alias("first"))
need = first.filter(pl.col("first") > START).sort("first")
print(f"\n有日K但起点晚于 2021-01-01 的码: {need.height}", flush=True)
print("  起点分布(月):", dict(sorted(Counter(r[:7] for r in need["first"].to_list()).items())), flush=True)

# 完全没K线 + 起点晚 的码合并 = 全部需补
need_all = sorted(no_db + need["code"].to_list())
print(f"\n需补日K的码总数: {len(need_all)}", flush=True)
# 区间内最后出现在快照的时间（判断退市/调出时点）
last_in_snap = seg.filter(pl.col("code").is_in(need_all)).group_by("code").agg(
    pl.col("snap_date").max().alias("last_snap"))
print("需补码最后一次出现在快照的分布(年-月):", flush=True)
dist = Counter(r[:7] for r in last_in_snap["last_snap"].to_list())
print(dict(sorted(dist.items())), flush=True)

(ROOT / ".workbuddy" / "zz500_2021_need.txt").write_text("\n".join(need_all), encoding="utf-8")
print(f"\n清单已存: .workbuddy/zz500_2021_need.txt ({len(need_all)} 码)", flush=True)
