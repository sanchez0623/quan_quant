# -*- coding: utf-8 -*-
"""验证缩窗口后新拉文件的数据范围（首只应为 2024-01 起）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

target = sys.argv[1] if len(sys.argv) > 1 else "000049"
p = Path(r"d:\Sanchez\AI\TraeProjects\quan_quant\data\minute5") / f"{target}.parquet"
if not p.exists():
    print(f"{target}.parquet 尚未落盘")
else:
    df = pl.read_parquet(p)
    print(f"{target}: {df['date'].min()} -> {df['date'].max()}  rows={df.height}")
    print("窗口确认：2024-01 起 ✓" if str(df["date"].min())[:4] == "2024"
          else "!!! 仍是 2023 窗口，需检查")
