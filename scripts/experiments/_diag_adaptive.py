# -*- coding: utf-8 -*-
"""诊断：trend 自适应分支在分钟语境的真实触发率（monkeypatch，不改引擎）。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_gateoff_oat import base_gate_on  # noqa: E402
from app.engine import risk as risk_mod  # noqa: E402
from app.engine import runner  # noqa: E402

STATS = Counter()


def wrapped(self, pos, bar):
    c = self.cfg
    if c.adaptive == "off" or not bar:
        STATS["off_or_no_bar"] += 1
        return 1.0
    if c.adaptive == "trend":
        ma = bar.get("adaptive_ma")
        close = bar.get("close")
        if ma is None or close is None:
            STATS["ma_or_close_none"] += 1
            return 1.0
        if close <= ma:
            STATS["k_tight(close<=ma)"] += 1
            return c.adaptive_k_tight
        slope = bar.get("adaptive_slope")
        if slope is None:
            STATS["slope_none(close>ma)"] += 1
            return 1.0
        if slope >= 0:
            STATS["k_loose(close>ma,slope>=0)"] += 1
            return c.adaptive_k_loose
        STATS["neutral(close>ma,slope<0)"] += 1
        return 1.0
    STATS["other_mode"] += 1
    return 1.0


risk_mod.RiskManager._adaptive_mult = wrapped


def main():
    cfg = base_gate_on()
    cfg["start_date"] = "2025-07-01"
    cfg["end_date"] = "2025-10-01"
    cfg["name"] = "diag_adaptive"
    rep = runner.run_backtest(cfg)
    m = rep.get("metrics") or {}
    print("回测完成：", {k: m.get(k) for k in ("total_return", "excess_return")})
    total = sum(STATS.values())
    print(f"分支触发统计（共 {total} 次调用）：")
    for k, v in STATS.most_common():
        print(f"  {k}: {v} ({v/max(total,1):.1%})")


if __name__ == "__main__":
    main()
