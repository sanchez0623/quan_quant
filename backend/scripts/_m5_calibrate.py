# -*- coding: utf-8 -*-
"""分钟线回测算力校准：150 只子集 + HALF_GATE 参数 + 2021-07-15~2026-09-07 单次耗时。"""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage0_anchors import _cfg, _fmt, _zz500_universe  # noqa: E402
from stage3_optimize import _apply, _load_half_gate_overrides  # noqa: E402

from app.engine import runner  # noqa: E402


def main():
    uni = _zz500_universe("2021-07-15")
    rng = random.Random(20260908)
    uni150 = sorted(rng.sample(uni, 150))
    cfg = _cfg("m5_calibration", uni150, start="2021-07-15", end="2026-09-07")
    cfg["period"] = "minute5"
    ov = _load_half_gate_overrides()
    ov[("params", "max_t_times")] = 4
    cfg = _apply(cfg, ov)
    t0 = time.time()
    rep = runner.run_backtest(cfg)
    m = rep.get("metrics", {}) or {}
    print(f"单次分钟回测（150只/5.2年）耗时 {time.time() - t0:,.0f}s", flush=True)
    print(f"收益 {_fmt(m.get('total_return'))} | 超额 {_fmt(m.get('excess_return'))} "
          f"| 回撤 {_fmt(m.get('max_drawdown'))} | 做T占比 {_fmt(m.get('t_pnl_share'))} "
          f"| 做T盈亏 {_fmt(m.get('t_pnl'), pct=False)}", flush=True)


if __name__ == "__main__":
    main()
