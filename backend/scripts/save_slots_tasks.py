# -*- coding: utf-8 -*-
"""槽位语境证伪任务落库（S8/S10 双段，不带🏷️——证伪对照组）。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT  # noqa: E402
from pulse_ratchet_oat import base_ratchet  # noqa: E402
from pulse_slots_oat import SLOT_VARIANTS, apply_slots  # noqa: E402

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
    cfg0 = base_ratchet()
    for name, spec in SLOT_VARIANTS.items():
        full = apply_slots(cfg0, spec, f"slots_{name}_full")
        save(f"槽位语境证伪-{name}({spec['holdings']}槽等权)-全区间(分钟)", full)
        oos = apply_slots({**cfg0, "start_date": OOS_SPLIT}, spec,
                          f"slots_{name}_oos")
        save(f"槽位语境证伪-{name}-OOS段(分钟)", oos)


if __name__ == "__main__":
    main()
