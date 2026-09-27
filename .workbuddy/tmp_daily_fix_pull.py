# -*- coding: utf-8 -*-
"""Phase A：2022 口径日K补拉（482 码，2022-01-04~最新）。
updater.update 官方路径：baostock 单连接串行 + 因子同源拉取展开 + 分批落库合并。"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import updater, store

codes = [c for c in (ROOT / ".workbuddy" / "zz500_daily_fix_codes.txt")
         .read_text(encoding="utf-8").splitlines() if c]
end = store.daily_latest_date(str(ROOT / "data"))
print(f"补拉 {len(codes)} 码, {end} 止", flush=True)

def cb(p, m):
    print(f"[{p:5.1f}%] {m}", flush=True)

stats = updater.update(scope="daily", codes=codes,
                       start_date="2022-01-04", end_date=end, progress_cb=cb)
print("\n=== DONE ===", flush=True)
for k, v in stats.items():
    print(f"{k}: {v}", flush=True)
