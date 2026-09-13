# -*- coding: utf-8 -*-
"""方案 E 语境对照任务落库（trade_tier_on+regime_b_on 双开，不带🏷️——证伪对照组）。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_regimeb_oat import apply_e  # noqa: E402

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
    cfg0 = base_gate_on()
    save("方案E证伪-tier+regimeB-全区间(分钟)",
         apply_e(cfg0, "regimeb_chk_full"))
    save("方案E证伪-tier+regimeB-OOS段(分钟)",
         apply_e({**cfg0, "start_date": OOS_SPLIT}, "regimeb_chk_oos"))


if __name__ == "__main__":
    main()
