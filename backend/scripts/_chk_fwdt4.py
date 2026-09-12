# -*- coding: utf-8 -*-
"""实证：全区间 FWD_T 的 params 是否真带 fwd_t=on、正向T是否触发。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import FWD, mk5  # noqa: E402
from app.engine import runner  # noqa: E402

cfg = mk5(FWD, False, "chk4_fwdt_full")
print("cfg.params.fwd_t =", repr(cfg["params"].get("fwd_t")))
print("cfg.period =", cfg["period"], "| universe_auto =", cfg["universe_auto"])
rep = runner.run_backtest(cfg)
tl = rep.get("trade_log") or []
fwd_cnt = sum(1 for t in tl if "正向T" in str(t.get("reason", "")))
print(f"全区间交易 {len(tl)} 笔，正向T {fwd_cnt} 笔")
reasons = Counter(str(t.get("reason", ""))[:14] for t in tl if "做T" in str(t.get("tag", "")))
for r, n in reasons.most_common(6):
    print(f"  {n:>4}  {r}")
