# -*- coding: utf-8 -*-
"""补拉中证500 2021-01-01~2023-03-27 日K + 因子（基于历史快照成分 660 码）。
走 updater.update 官方路径：降级链拉日线 + 因子展开 + 全表增量合并。
"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import updater

codes = (ROOT / ".workbuddy" / "zz500_2021_need.txt").read_text(encoding="utf-8").splitlines()
codes = [c for c in codes if c]
print(f"补拉码数: {len(codes)}", flush=True)

def cb(p, m):
    print(f"[{p:5.1f}%] {m}", flush=True)

stats = updater.update(scope="daily", codes=codes,
                       start_date="2021-01-01", end_date="2023-03-27",
                       progress_cb=cb)
print("\n=== DONE ===", flush=True)
for k, v in stats.items():
    print(f"{k}: {v}", flush=True)
