# -*- coding: utf-8 -*-
"""诊断：k_loose 覆盖是否真的传入引擎（迷你回测 ×2 + RiskManager 属性打印）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_gateoff_oat import base_gate_on  # noqa: E402
from app.engine import risk as risk_mod  # noqa: E402
from app.engine import runner  # noqa: E402

SEEN: list[float] = []
_orig_init = risk_mod.RiskManager.__init__


def patched_init(self, cfg, *a, **kw):
    _orig_init(self, cfg, *a, **kw)
    SEEN.append(self.cfg.adaptive_k_loose)


risk_mod.RiskManager.__init__ = patched_init


def run(k_loose: float) -> dict:
    cfg = base_gate_on()
    cfg["risk_config"]["adaptive_k_loose"] = k_loose
    cfg["start_date"] = "2025-07-01"
    cfg["end_date"] = "2025-08-01"
    cfg["name"] = f"diag_kloose_{k_loose}"
    SEEN.clear()
    rep = runner.run_backtest(cfg)
    m = rep.get("metrics") or {}
    print(f"k_loose 传入 {k_loose} → 引擎内实例值 {SEEN} → "
          f"total {m.get('total_return'):+.4%} excess {m.get('excess_return'):+.4%}",
          flush=True)
    return m


def main():
    m1 = run(1.8)
    m2 = run(1.2)
    same = abs(m1.get("total_return", 0) - m2.get("total_return", 0)) < 1e-12
    print("两次结果相同：" , same)


if __name__ == "__main__":
    main()
