# -*- coding: utf-8 -*-
"""查看 38 码缺口清单 + 股票属性（是否退市/科创板/段详情）"""
import json, sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store

plan = json.loads((ROOT / ".workbuddy" / "zz500_m5_need.json").read_text(encoding="utf-8"))
basic = store.read_stock_basic(str(ROOT / "data"))
basic_map = {}
if basic is not None:
    for r in basic.to_dicts():
        basic_map[r["code"]] = r

daily = store.read_daily(None, str(ROOT / "data"))
for c in sorted(plan):
    b = basic_map.get(c, {})
    name = b.get("name", "?")
    delisted = b.get("delisted", "?")
    board = "科创" if c.startswith(("688", "689")) else ("创业" if c.startswith(("300", "301")) else "主板")
    segs = plan[c]
    n_days = sum((pl.read_parquet(str(ROOT / "data/daily.parquet"), columns=["date"])
                  .filter((pl.col("date") >= s) & (pl.col("date") <= e)).height) for s, e in segs)
    print(f"{c} {name} [{board}] delisted={delisted} 段数={len(segs)} 段: {segs[:4]}")
