# -*- coding: utf-8 -*-
"""补拉 zz500 历史成分码 2023-03-28~2026-09-07 缺口段日K+因子（300 码）。
剔除 4 只 2023-03-28 前已退市的码（002013/600068/600260/600291，无数据可拉）。
"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import updater

codes = [c for c in (ROOT / ".workbuddy" / "zz500_gap2023_need.txt")
         .read_text(encoding="utf-8").splitlines() if c]
skip = {"002013", "600068", "600260", "600291"}
codes = [c for c in codes if c not in skip]
print(f"补拉码数: {len(codes)}", flush=True)

def cb(p, m):
    print(f"[{p:5.1f}%] {m}", flush=True)

stats = updater.update(scope="daily", codes=codes,
                       start_date="2023-03-28", end_date="2026-09-07",
                       progress_cb=cb)
print("\n=== DONE ===", flush=True)
for k, v in stats.items():
    print(f"{k}: {v}", flush=True)
