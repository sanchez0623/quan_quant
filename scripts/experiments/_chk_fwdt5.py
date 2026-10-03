# -*- coding: utf-8 -*-
"""定位顺序效应：连续两次 run_backtest，第二次的 fwd_t 在 cfg/apply_param_defaults/结果三层检查。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import FWD, base_cfg, mk5  # noqa: E402
from app.engine import runner  # noqa: E402


def fwd_count(rep):
    return sum(1 for t in rep.get("trade_log") or []
               if "正向T" in str(t.get("reason", "")))


cfg1 = mk5({}, False, "c1_base")
rep1 = runner.run_backtest(cfg1)
print(f"run1(BASE): 正向T {fwd_count(rep1)} 笔", flush=True)

cfg2 = mk5(FWD, False, "c2_fwd")
print(f"cfg2.params.fwd_t = {cfg2['params'].get('fwd_t')!r}", flush=True)
applied = runner.apply_param_defaults("momentum_slot", cfg2.get("params") or {})
print(f"apply_param_defaults 后 fwd_t = {applied.get('fwd_t')!r}", flush=True)
rep2 = runner.run_backtest(cfg2)
print(f"run2(FWD_T): 正向T {fwd_count(rep2)} 笔", flush=True)

# 反序对照：先 FWD_T 后 BASE
cfg3 = mk5(FWD, False, "c3_fwd_first")
rep3 = runner.run_backtest(cfg3)
print(f"run3(新进程外无法测，先FWD): 正向T {fwd_count(rep3)} 笔", flush=True)
cfg4 = mk5({}, False, "c4_base_after")
rep4 = runner.run_backtest(cfg4)
print(f"run4(BASE 后跑): 正向T {fwd_count(rep4)} 笔", flush=True)
