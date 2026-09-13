# -*- coding: utf-8 -*-
"""做T层采纳形态落库：基座 + asym_bias=0.0（分钟语境），全区间 + OOS。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT  # noqa: E402
from pulse_t_oat import base_m5  # noqa: E402

from app import db  # noqa: E402
from app.engine import runner  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"


def save(name: str, cfg: dict) -> None:
    rep = runner.run_backtest(cfg)
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


def main():
    REPORTS.mkdir(exist_ok=True)
    cfg = base_m5()
    cfg["params"]["asym_bias"] = 0.0
    full = json.loads(json.dumps(cfg))
    full["name"] = "pulse_t_adopted_full"
    save("做T层采纳形态-asym_bias0-全区间(分钟)", full)
    oos = json.loads(json.dumps(cfg))
    oos["start_date"] = OOS_SPLIT
    oos["name"] = "pulse_t_adopted_oos"
    save("做T层采纳形态-asym_bias0-OOS段(分钟)", oos)


if __name__ == "__main__":
    main()
