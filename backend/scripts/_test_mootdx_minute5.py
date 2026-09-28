# -*- coding: utf-8 -*-
"""mootdx 5分钟线拉取可靠性测试（用户授权 2026-09-28）。

- 并发硬上限 3 线程（项目规则）；票数 12 只，请求量 ~几十次
- 测：单线程基准耗时 / 数据完整性(每日48根) / 与本地 parquet 逐bar一致性 /
  3 线程并发吞吐与错误率 / 服务器端深度边界
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app.data.sources import MootdxSource
from app.data import store

src = MootdxSource()
print(f"mootdx available={src.available()}")
assert src.available()
print(f"health_check={src.health_check()}")

CODES = ["000001", "600000", "300750", "688981"]
START, END = "2024-01-01", "2026-09-26"

# ---------- 1) 单线程基准 + 完整性 ----------
print("\n===== 1) 单线程基准 =====")
results = {}
for code in CODES:
    t0 = time.time()
    df = src.get_minute5(code, START, END)
    dt = time.time() - t0
    if df is None or df.height == 0:
        print(f"  {code}: 无数据 ({dt:.1f}s)")
        continue
    days = df.select(pl.col("date").str.slice(0, 10).alias("d")).group_by("d").len()
    per_day = days["len"].mean()
    n_bad = days.filter(pl.col("len") < 48).height
    results[code] = df
    print(f"  {code}: {df.height} 行  {df['date'].min()} ~ {df['date'].max()}  "
          f"日均 {per_day:.1f} 根  <48根的交易日 {n_bad}/{days.height}  耗时 {dt:.1f}s")

# ---------- 2) 与本地 parquet 逐 bar 一致性 ----------
print("\n===== 2) 与本地 parquet 一致性（000001 深度票） =====")
local = store.read_minute5("000001", START, END)
if local is not None and "000001" in results:
    remote = results["000001"]
    j = local.join(remote.select(["date", "open", "high", "low", "close", "volume"]),
                   on="date", how="inner", suffix="_r")
    n = j.height
    if n:
        diffs = {c: int((j[c] - j[f"{c}_r"]).abs().gt(0.001).sum())
                 for c in ["open", "high", "low", "close"]}
        vdiff = int((j["volume"] - j["volume_r"]).abs().gt(1).sum())
        print(f"  重叠 bar {n} 根：OHLC 不一致 {diffs}，volume 不一致 {vdiff}")
        print(f"  本地独有 {local.height - n} 根，远程独有 {remote.height - n} 根")
else:
    print("  跳过（本地无 000001 数据或远程拉取失败）")

# ---------- 3) 3 线程并发（项目规则上限） ----------
print("\n===== 3) 并发 3 线程（9 票 × 2 轮） =====")
CODES9 = ["000001", "600000", "300750", "688981", "000858", "601318",
          "002594", "600519", "688111"]
for rnd in range(2):
    t0 = time.time()
    errs = []

    def pull(code):
        t1 = time.time()
        try:
            df = src.get_minute5(code, START, END)
            return (code, df.height if df is not None else 0, time.time() - t1, None)
        except Exception as e:
            return (code, 0, time.time() - t1, repr(e)[:60])

    with ThreadPoolExecutor(max_workers=3) as ex:
        out = list(ex.map(pull, CODES9))
    dt = time.time() - t0
    ok = [r for r in out if r[3] is None and r[1] > 0]
    print(f"  第{rnd + 1}轮: 成功 {len(ok)}/9  总耗时 {dt:.1f}s  "
          f"平均单票 {sum(r[2] for r in out) / 9:.1f}s  错误 {[r[3] for r in out if r[3]]}")

# ---------- 4) 深度边界 ----------
print("\n===== 4) 服务器端深度边界（000001 不限起始日） =====")
t0 = time.time()
df = src.get_minute5("000001", "2000-01-01", "2026-09-26")
if df is not None:
    print(f"  最早回捞至 {df['date'].min()}，共 {df.height} 行，耗时 {time.time()-t0:.1f}s")
else:
    print("  拉取失败")
