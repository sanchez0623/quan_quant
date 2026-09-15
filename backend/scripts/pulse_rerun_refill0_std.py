# -*- coding: utf-8 -*-
"""refill0 标准近似窗重跑：2022-09-01~2026-08-31（用户指定段），两复现任务 config 其余原样。"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db                       # noqa: E402
from app.engine import runner            # noqa: E402
from pulse_ratchet_oat import _score     # noqa: E402

SEG = ("2022-09-01", "2026-08-31")
TARGETS = [
    "bt_af9ee940144e",
    "bt_3cffb42e189a",
]
REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"


def pct(v) -> str:
    return f"{v:+.2%}" if isinstance(v, (int, float)) else "-"


def main():
    t0 = time.time()
    for old_tid in TARGETS:
        old = db.get_task(old_tid)
        cfg = json.loads(json.dumps((old.get("payload") or {}).get("config") or {}))
        cfg["pool_refill_min"] = 0
        cfg["start_date"], cfg["end_date"] = SEG
        new_name = f"影线承接复现-refill0-标准近似窗-{old_tid[-4:]}(分钟)"
        cfg["name"] = new_name
        print(f"[回测] {new_name}（源 {old_tid}, refill=0, 段 {SEG[0]}~{SEG[1]}）...", flush=True)
        rep = runner.run_backtest(cfg)
        m = rep.get("metrics", {}) or {}
        sc = _score(rep)
        tid = "bt_" + uuid.uuid4().hex[:12]
        path = REPORTS / f"{tid}.json"
        path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
        payload = {"strategy_id": cfg.get("strategy_id", ""),
                   "period": cfg.get("period", ""), "config": cfg,
                   "report_path": str(path)}
        db.create_task(tid, new_name, "backtest", payload, tag="重点")
        db.save_report(tid, str(path))
        db.update_task(tid, status="success", progress=100, message="")
        print(f"  → 落库 {tid}: score {sc['score']:.4f}｜收益 {pct(m.get('total_return'))}｜"
              f"超额 {pct(m.get('excess_return'))}｜回撤 {pct(m.get('max_drawdown'))}", flush=True)
    print(f"完成，用时 {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
