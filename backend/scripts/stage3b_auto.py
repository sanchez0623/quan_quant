# -*- coding: utf-8 -*-
"""阶段 3b 验证：阶段 3 最优参数 × 动态换血（universe_auto）组合，落库对照。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stage0_anchors import END_DEFAULT, START_DEFAULT, _cfg, _zz500_universe  # noqa: E402
from scripts.stage4_crosspool import OPT  # noqa: E402

from app import db  # noqa: E402
from app.engine import runner  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"


def make_cfg_auto(name, start, end):
    """动态换血 + 阶段 3 寻优档（L2 基底）。"""
    cfg = _cfg(name, [], universe_auto=True, pool_gate=True, start=start, end=end)
    for k, v in OPT.items():
        if k.startswith("params."):
            cfg["params"][k.split(".", 1)[1]] = v
        elif k.startswith("risk."):
            cfg["risk_config"][k.split(".", 1)[1]] = v
        elif k.startswith("top."):
            cfg[k.split(".", 1)[1]] = v
    return cfg


def run_and_save(name, start, end):
    cfg = make_cfg_auto(name, start, end)
    rep = runner.run_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    (REPORTS / f"{tid}.json").write_text(
        json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(REPORTS / f"{tid}.json")}
    db.create_task(tid, name, "backtest", payload)
    db.save_report(tid, str(REPORTS / f"{tid}.json"))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    print(f"{tid}  {name}  收益 {m.get('total_return'):+.2%}  超额 {m.get('excess_return'):+.2%}",
          flush=True)


def main():
    REPORTS.mkdir(exist_ok=True)
    run_and_save("v5阶段3b-寻优参数×动态换血-全区间", START_DEFAULT, END_DEFAULT)
    run_and_save("v5阶段3b-寻优参数×动态换血-OOS段", "2025-07-03", END_DEFAULT)


if __name__ == "__main__":
    main()
