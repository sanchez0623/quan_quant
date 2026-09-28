# -*- coding: utf-8 -*-
"""对质 seek-code：全库 minute5 文件首bar日期分布 + mtime + 抽样真实缺口
1) 每个分钟文件：min(date)（真实首bar）、mtime
2) 首bar年-月分布 vs seek-code 表（2020-01:49 / 2021-01:693 / 2024-01:3841 ...）
3) 抽 5 只 C 阶段补过历史日K的老票：日K 2022-2023 交易日数 vs 分钟 2022-2023 bar 数
"""
import sys, json, pathlib, time, datetime
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

M5DIR = ROOT / "data" / "minute5"
DATA = str(ROOT / "data")
OUT = ROOT / ".workbuddy" / "m5_firstbar_scan.jsonl"
OUT.unlink(missing_ok=True)

files = sorted(M5DIR.glob("*.parquet"))
print(f"分钟文件数: {len(files)}", flush=True)

with OUT.open("a", encoding="utf-8") as f:
    for i, p in enumerate(files):
        try:
            m5 = pl.read_parquet(p, columns=["date"])
            first = str(m5["date"].min())[:10]
            last = str(m5["date"].max())[:10]
        except Exception:
            first, last = "CORRUPT", "CORRUPT"
        mtime = time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))
        f.write(json.dumps({"code": p.stem, "first": first, "last": last, "mtime": mtime},
                           ensure_ascii=False) + "\n")
        if (i + 1) % 500 == 0:
            print(f"  扫描 {i + 1}/{len(files)}", flush=True)
f.flush() if False else None

rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
starts = Counter(r["first"][:7] for r in rows)
print("\n[首bar年-月分布] (seek-code 声称: 2024-01 有 3841)", flush=True)
for k, v in sorted(starts.items()):
    print(f"    {k}: {v}", flush=True)

mt = Counter(r["mtime"] for r in rows)
print("\n[文件mtime分布(按日)]:", flush=True)
for k, v in sorted(mt.items()):
    print(f"    {k}: {v}", flush=True)

# 抽样对质：5 只 C 阶段补过历史日K的老票
daily_all = store.read_daily(None, DATA)
samples = ["600463", "000032", "600004", "002007", "600711"]
print("\n[抽样对质] 日K 2022-01-04~2023-12-29 交易日 vs 分钟实际bar覆盖:", flush=True)
for c in samples:
    d = daily_all.filter((pl.col("code") == c) & (pl.col("date") >= "2022-01-04")
                         & (pl.col("date") <= "2023-12-29") & (pl.col("volume") > 0))
    m5p = M5DIR / f"{c}.parquet"
    if m5p.exists():
        m5 = pl.read_parquet(m5p, columns=["date"])
        m_sub = m5.filter((pl.col("date") >= "2022-01-04") & (pl.col("date") < "2023-12-30"))
        m_days = m_sub["date"].str.slice(0, 10).unique().sum() if False else m_sub["date"].str.slice(0, 10).n_unique()
        print(f"  {c}: 日K成交日 {d.height} 天 | 分钟2022-23覆盖 {m_days} 天 "
              f"(首bar {str(m5['date'].min())[:10]})", flush=True)
    else:
        print(f"  {c}: 无分钟文件", flush=True)
