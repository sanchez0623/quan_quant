# -*- coding: utf-8 -*-
"""P0 采纳形态落库：基座 + P4（slot_rotation_on=on, slot_stale_days=5），全区间 + OOS。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_oat import OOS_SPLIT, base_cfg, run_score  # noqa: E402

from app import db  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"
OV = {("params", "slot_rotation_on"): "on", ("params", "slot_stale_days"): 5}


def save(name: str, cfg: dict) -> None:
    rep = runner_run(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, name, "backtest", payload)
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    print(f"{tid}  {name}  收益 {m.get('total_return'):+.2%}  "
          f"超额 {m.get('excess_return'):+.2%}", flush=True)


def runner_run(cfg: dict) -> dict:
    from app.engine import runner
    return runner.run_backtest(cfg)


def main():
    REPORTS.mkdir(exist_ok=True)
    cfg = base_cfg()
    for (w, k), v in OV.items():
        cfg["params"][k] = v
    full = json.loads(json.dumps(cfg))
    full["name"] = "pulse_adopted_full"
    save("P0采纳形态-轮动stale5-全区间", full)
    oos = json.loads(json.dumps(cfg))
    oos["start_date"] = OOS_SPLIT
    oos["name"] = "pulse_adopted_oos"
    save("P0采纳形态-轮动stale5-OOS段", oos)


if __name__ == "__main__":
    main()
