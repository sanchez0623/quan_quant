# -*- coding: utf-8 -*-
"""最终验证：967 个 zz500 历史成分码（2021-01 起快照并集）
1) 日K起点（是否 2021-01-04 起，晚于此的是否 IPO 新股）
2) 日K连续性（在市窗口内交易日历 vs 库内行，缺行=中断）
3) 5分钟K覆盖（真实成交日 vol>0 vs 分钟文件，复用最新探查 gap_runs.jsonl）
4) 复权因子覆盖（每日一行 vs 日线日期数）
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

hist = pl.read_parquet(ROOT / "data" / "index_constituents_history.parquet")
seg = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") >= START))
codes = sorted(seg["code"].unique().to_list())
code_set = set(codes)
print(f"口径: zz500 2021-01 起快照并集 {len(codes)} 码, 区间 {START}~{END}", flush=True)

daily = store.read_daily(codes, str(ROOT / "data"))
cal = store.read_calendar(str(ROOT / "data"))
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]
cal_set = set(cal_dates)
print(f"交易日历区间内交易日数: {len(cal_dates)}", flush=True)

basic = store.read_stock_basic(str(ROOT / "data"))
delisted = set(basic.filter(pl.col("delisted"))["code"].to_list()) if "delisted" in basic.columns else set()

# ---------- 1) 日K起点 ----------
g = daily.group_by("code").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"),
                               pl.col("date").count().alias("rows"))
g = g.filter(pl.col("code").is_in(code_set))
first_map = dict(zip(g["code"].to_list(), g["first"].to_list()))
last_map = dict(zip(g["code"].to_list(), g["last"].to_list()))

early = [c for c in codes if first_map.get(c, "9999") <= "2021-01-04"]
late = sorted([(c, first_map[c]) for c in codes if first_map.get(c, "9999") > "2021-01-04"])
print(f"\n[1] 日K起点 <= 2021-01-04: {len(early)} 码 | 晚于: {len(late)} 码", flush=True)
for c, f in late:
    print(f"    {c}: 首日 {f}", flush=True)

# ---------- 2) 日K连续性（在市窗口内缺行） ----------
dates_by_code = (daily.filter(pl.col("code").is_in(code_set))
                 .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())
have_map = {r["code"]: set(r["ds"]) for r in dates_by_code}
missing_rows = {}
for c in codes:
    have = have_map.get(c)
    if not have:
        missing_rows[c] = -1
        continue
    f, l = min(have), max(have)
    window = [d for d in cal_dates if f <= d <= l]
    miss = [d for d in window if d not in have]
    if miss:
        missing_rows[c] = len(miss)
bad_daily = {c: n for c, n in missing_rows.items() if n > 0}
print(f"\n[2] 日K在市窗口内缺行码数: {len(bad_daily)}", flush=True)
for c, n in sorted(bad_daily.items(), key=lambda x: -x[1])[:15]:
    print(f"    {c}: 缺 {n} 个交易日 (delisted={c in delisted})", flush=True)
nodata = [c for c, n in missing_rows.items() if n == -1]
if nodata:
    print(f"    完全无日K: {nodata}", flush=True)

# ---------- 3) 5分钟K覆盖（复用最新探查结果） ----------
runs_file = WORK / "zz500_m5_gap_runs.jsonl"
m5_gap_total, m5_gap_codes = 0, {}
for line in runs_file.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    if r["code"] in code_set and r["gap_days"] > 0:
        m5_gap_codes[r["code"]] = r["gap_days"]
        m5_gap_total += r["gap_days"]
print(f"\n[3] 5分钟K: 真实成交日层面有缺口码数 {len(m5_gap_codes)}, 共 {m5_gap_total} 天", flush=True)
for c, n in sorted(m5_gap_codes.items()):
    print(f"    {c}: {n} 天 (delisted={c in delisted})", flush=True)

# ---------- 4) 复权因子覆盖 ----------
adj = store.read_adj_factor(codes, str(ROOT / "data"))
adj_cnt = (adj.group_by("code").agg(pl.col("date").count().alias("n")).to_dicts())
adj_map = {r["code"]: r["n"] for r in adj_cnt}
no_adj, less_adj = [], []
for c in codes:
    n_d = g.filter(pl.col("code") == c)["rows"][0] if g.filter(pl.col("code") == c).height else 0
    n_a = adj_map.get(c, 0)
    if n_a == 0:
        no_adj.append(c)
    elif n_a < n_d:
        less_adj.append((c, n_d, n_a))
print(f"\n[4] 复权因子: 无因子码 {len(no_adj)} | 因子行数<日K行数 {len(less_adj)}", flush=True)
for c, nd, na in less_adj[:10]:
    print(f"    {c}: 日K {nd} 行 vs 因子 {na} 行", flush=True)

print("\n=== 总结 ===", flush=True)
print(f"日K: {len(early)}/{len(codes)} 从 2021-01-04(或更早) 起, 起点晚的 {len(late)} 码, 缺行 {len(bad_daily)} 码", flush=True)
print(f"5分钟K: 缺口 {len(m5_gap_codes)} 码 / {m5_gap_total} 天（源端无数据）", flush=True)
print(f"因子: 缺 {len(no_adj)} 码, 行数不足 {len(less_adj)} 码", flush=True)
