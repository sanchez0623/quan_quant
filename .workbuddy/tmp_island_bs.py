# -*- coding: utf-8 -*-
"""放大区间测试：002920 / 688065 的 5分钟线源端覆盖"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data.sources import BaostockSource, MootdxSource

bs = BaostockSource()
md = MootdxSource()
cases = [
    ("002920", "2022-02-14", "2022-02-25"),   # 含孤点 2022-02-21
    ("688065", "2023-06-12", "2023-06-16"),   # 含孤点 2023-06-15
]
for c, s, e in cases:
    for tag, src in (("baostock", bs), ("mootdx", md)):
        df = src.get_minute5(c, s, e)
        if df is None or df.height == 0:
            print(f"{c} [{tag}] {s}~{e}: 空", flush=True)
        else:
            days = sorted(set(df["date"].str.slice(0, 10).unique().to_list()))
            print(f"{c} [{tag}] {s}~{e}: {df.height} bar, {len(days)} 天: {days}", flush=True)
