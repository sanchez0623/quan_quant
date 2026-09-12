# -*- coding: utf-8 -*-
"""把 v5 最终形态（阶段 3 寻优最优）落库为正式任务，供界面复查。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stage0_anchors import END_DEFAULT, START_DEFAULT, BENCHMARK, _cfg, _zz500_universe  # noqa: E402
from scripts.stage4_crosspool import OPT  # noqa: E402

from app.engine import runner  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"


def make_cfg(name, universe, start, end):
    """L2 基底（与 stage3 final 一致）+ 寻优档叠加。"""
    cfg = _cfg(name, universe, pool_gate=True, start=start, end=end)
    for k, v in OPT.items():
        if k.startswith("params."):
            cfg["params"][k.split(".", 1)[1]] = v
        elif k.startswith("risk."):
            cfg["risk_config"][k.split(".", 1)[1]] = v
        elif k.startswith("top."):
            cfg[k.split(".", 1)[1]] = v
    return cfg


def run_and_save(name, start, end):
    cfg = make_cfg(name, _zz500_universe(START_DEFAULT), start, end)
    rep = runner.run_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    (REPORTS / f"{tid}.json").write_text(
        json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    m = rep.get("metrics") or {}
    print(f"{tid}  {name}  收益 {m.get('total_return'):+.2%}  超额 {m.get('excess_return'):+.2%}",
          flush=True)


def main():
    REPORTS.mkdir(exist_ok=True)
    run_and_save("v5最终形态-阶段3寻优最优-全区间", START_DEFAULT, END_DEFAULT)
    run_and_save("v5最终形态-阶段3寻优最优-OOS段", "2025-07-03", END_DEFAULT)


if __name__ == "__main__":
    main()


def main():
    REPORTS.mkdir(exist_ok=True)
    run_and_save("v5最终形态-阶段3寻优最优-全区间", START_DEFAULT, END_DEFAULT)
    run_and_save("v5最终形态-阶段3寻优最优-OOS段", "2025-07-03", END_DEFAULT)


if __name__ == "__main__":
    main()
