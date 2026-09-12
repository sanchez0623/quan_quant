# -*- coding: utf-8 -*-
"""验证 baostock 是否解除黑名单 + 事件级因子质量（300144 对照修复值 16.495526）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import sources  # noqa: E402

bs = next((s for s in sources.SOURCES if s.name == "baostock"), None)
print("baostock available:", bs.available() if bs else None)
if bs is None:
    raise SystemExit(1)

df = bs.get_adj_factor("300144")
ok = df is not None and df.height
print("get_adj_factor 300144:", "OK" if ok else "FAIL(None=失败不占位)")
if ok:
    print("事件数:", df.height)
    print("首事件:", df.row(0, named=True))
    print("末事件:", df.row(df.height - 1, named=True))
    print("最新因子（对照 2026-08-31 修复值 16.495526）:", df["adj_factor"].to_list()[-1])
