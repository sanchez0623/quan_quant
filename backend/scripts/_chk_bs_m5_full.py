# -*- coding: utf-8 -*-
"""诊断：baostock 全窗口分钟线是否有行数截断（B 步数据完整性验证）
对照基准：000014.parquet 库内文件 43200 行（2023-01-03 -> 2026-09-17）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import sources

src = sources.BaostockSource()
df = src.get_minute5("000014", "2023-01-01", "2099-12-31")
if df is None:
    print("baostock 返回 None（该票全窗口不可用，B 步靠 mootdx 兜底）")
else:
    print(f"rows={df.height} range={df['date'].min()} -> {df['date'].max()}")
    print("结论：完整无截断" if df.height > 40000
          else f"结论：疑似截断（{df.height} < 40000 行，需改分段拉取）")
