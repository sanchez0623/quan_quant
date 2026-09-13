# -*- coding: utf-8 -*-
"""成本基准 WAVG 语境对照任务落库（atr_cost_base=wavg 双段，不带🏷️——未采纳对照组）。"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT  # noqa: E402
from pulse_costbase_oat import apply_wavg, base_gate_on  # noqa: E402

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
    save("成本基准对照-wavg加权平均-全区间(分钟)",
         apply_wavg(cfg0, "wavg_chk_full"))
    save("成本基准对照-wavg加权平均-OOS段(分钟)",
         apply_wavg({**cfg0, "start_date": OOS_SPLIT}, "wavg_chk_oos"))


if __name__ == "__main__":
    main()
