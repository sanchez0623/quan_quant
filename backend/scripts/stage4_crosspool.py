# -*- coding: utf-8 -*-
"""阶段 4 验收：阶段 3 寻优最优配置的跨池检验（随机300，选股无关性）。"""
import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0_anchors import END_DEFAULT, START_DEFAULT, BENCHMARK, _zz500_universe  # noqa: E402

from app.engine import runner  # noqa: E402

OPT = {  # 阶段 3 最优（HALF_GATE + 22 项寻优档）
    "params.add_cooldown": 1, "params.add_scale": 0.8, "params.atr_stop_k": -5,
    "params.base_pct_max": 50, "params.crash_abs_cap": 45, "params.crash_vol_n": 90,
    "params.exit_confirm_days": 2, "params.exit_need": 3, "params.ma_fast": 20,
    "params.macd_fast": 20, "params.macd_signal": 15, "params.macd_slow": 26,
    "params.max_adds": 2, "params.max_holdings": 6, "params.mom_short": 20,
    "params.out_top_days": 0, "params.pool_n": 16, "params.w_accel": 0.1,
    "params.w_short": 0.4, "risk.adaptive": "trend",
    "risk.stop_loss_mode": "atr_trailing", "top.pool_gate": True,
}


def make_cfg(name, universe, start, end):
    cfg = {
        "name": name, "strategy_id": "momentum_slot",
        "params": {}, "risk_config": {}, "universe": universe,
        "universe_auto": False, "auto_index": [], "auto_boards": [],
        "start_date": start, "end_date": end, "period": "daily",
        "initial_capital": 3_000_000.0, "benchmark": BENCHMARK,
        "pool_gate": True, "pool_gate_enter_th": 0.15,
    }
    for k, v in OPT.items():
        if k.startswith("params."):
            cfg["params"][k.split(".", 1)[1]] = v
        elif k.startswith("risk."):
            cfg["risk_config"][k.split(".", 1)[1]] = v
        elif k.startswith("top."):
            cfg[k.split(".", 1)[1]] = v
    return cfg


def run(name, universe, start, end):
    r = runner.run_backtest(make_cfg(name, universe, start, end))
    m = r.get("metrics") or {}
    return {"name": name, "total_return": m.get("total_return"),
            "bench": m.get("benchmark_return"), "excess": m.get("excess_return"),
            "mdd": m.get("max_drawdown"), "sharpe": m.get("sharpe") or 0.0}


def main():
    u500 = _zz500_universe(START_DEFAULT)
    rng = random.Random(20260908)
    u300 = sorted(rng.sample(u500, 300))
    rows = [
        run("opt-pool300-full", u300, START_DEFAULT, END_DEFAULT),
        run("opt-pool300-oos", u300, "2025-07-03", END_DEFAULT),
    ]
    out = Path(__file__).parent / "out" / f"stage4_crosspool_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        tr, be, ex = r["total_return"], r["bench"], r["excess"]
        print(f"[{r['name']}] 收益 {tr:+.2%} | 基准 {be:+.2%} | 超额 {ex:+.2%} | "
              f"回撤 {r['mdd']:+.2%} | 夏普 {r['sharpe']:.2f}", flush=True)
    print(f"已存 {out}", flush=True)


if __name__ == "__main__":
    main()
