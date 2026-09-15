# -*- coding: utf-8 -*-
"""refill0 修正重跑：两个中招复现任务（top50+refill2 误配）在 pool_refill_min=0 下重跑。

bt_af9ee940144e（2022-05-01~2026-04-30）与 bt_3cffb42e189a（2022-04-01~2026-03-31）
的 config 除 refill 外原样保留；落库三件套 + tag=重点；输出新旧结果对照。
"""
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

TARGETS = [
    ("bt_af9ee940144e", "影线承接复现-refill0修正-2205窗(分钟)"),
    ("bt_3cffb42e189a", "影线承接复现-refill0修正-2204窗(分钟)"),
]
REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"


def metrics_of(rep: dict) -> dict:
    m = rep.get("metrics", {}) or {}
    return {"total_return": m.get("total_return"), "excess_return": m.get("excess_return"),
            "max_drawdown": m.get("max_drawdown")}


def pct(v) -> str:
    return f"{v:+.2%}" if isinstance(v, (int, float)) else "-"


def main():
    t0 = time.time()
    for old_tid, new_name in TARGETS:
        old = db.get_task(old_tid)
        cfg = json.loads(json.dumps((old.get("payload") or {}).get("config") or {}))
        cfg["pool_refill_min"] = 0
        cfg["name"] = new_name
        print(f"[回测] {new_name}（源 {old_tid}, refill 2→0）...", flush=True)
        rep = runner.run_backtest(cfg)
        m_new = metrics_of(rep)
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
        # 旧报告（refill2）对照
        old_path = (old.get("payload") or {}).get("report_path")
        m_old = {}
        if old_path and Path(old_path).exists():
            try:
                m_old = metrics_of(json.loads(Path(old_path).read_text(encoding="utf-8")))
            except Exception:
                pass
        print(f"  → 新任务 {tid}（refill=0）: score {sc['score']:.4f}｜"
              f"收益 {pct(m_new.get('total_return'))}｜超额 {pct(m_new.get('excess_return'))}｜"
              f"回撤 {pct(m_new.get('max_drawdown'))}", flush=True)
        print(f"    旧 {old_tid}（refill=2）: 收益 {pct(m_old.get('total_return'))}｜"
              f"超额 {pct(m_old.get('excess_return'))}｜回撤 {pct(m_old.get('max_drawdown'))}", flush=True)
    print(f"完成，用时 {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
