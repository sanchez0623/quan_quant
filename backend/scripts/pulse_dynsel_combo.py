# -*- coding: utf-8 -*-
"""组合验证：TOP50 × 换血线 1/2/3（universe_auto=on + auto_top_x=50 + pool_refill_min∈{1,2,3}）。

用户拍板跑的组合叠加验证（"过线≠可叠加"铁律：两组增益必须单独验证组合）。
对照三方：TOP50 包（refill=0）｜换血线单档（top30）｜off 基座。
R2 已有缓存（COMBO_* 键），本轮补 R1/R3；落库一体。
"""
import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_dynsel_oat import mk_dyn  # noqa: E402
from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_dynsel_combo_rows.jsonl"

# 三方对照基准（来自 pulse_dynsel_rows.jsonl 已落库结果）
REFS = {
    "TOP50包(refill0)": {"full": {"score": -0.0534, "excess_return": 0.2372},
                         "oos": {"score": 0.1820, "excess_return": 0.4439}},
    "换血线2档(top30)": {"full": {"score": -0.2205, "excess_return": 0.1342},
                         "oos": {"score": -0.0266, "excess_return": 0.1765}},
    "off基座": {"full": {"score": -0.2205, "excess_return": 0.1342},
                "oos": {"score": -0.0266, "excess_return": 0.1765}},
}


def main():
    t0 = time.time()
    OUT_DIR.mkdir(exist_ok=True)
    done: dict[str, dict] = {}
    if ROWS_JSONL.exists():
        for line in ROWS_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done[r["combo"]] = r
            except Exception:
                continue
    # R2 旧缓存键别名（COMBO_* → COMBOR2_*）
    for tag in ("全区间", "OOS段"):
        if f"COMBO_{tag}" in done:
            done[f"COMBOR2_{tag}"] = done[f"COMBO_{tag}"]

    conn = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
    task_names = {x[0] for x in conn.execute("SELECT name FROM tasks")}
    conn.close()

    def run_and_log_save(cfg: dict, combo: str, task_name: str) -> dict:
        if combo in done:
            print(f"[缓存] {combo}", flush=True)
            return done[combo]
        print(f"[回测] {combo} ...", flush=True)
        from app.engine import runner
        rep = runner.run_backtest(cfg)
        s = _score(rep)
        m = rep.get("metrics", {}) or {}
        r = {"combo": combo, "score": s["score"], "total_return": m.get("total_return"),
             "excess_return": m.get("excess_return"), "max_drawdown": m.get("max_drawdown")}
        if task_name not in task_names:
            reports = Path(__file__).resolve().parents[2] / "data" / "reports"
            reports.mkdir(exist_ok=True)
            tid = "bt_" + uuid.uuid4().hex[:12]
            path = reports / f"{tid}.json"
            path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
            payload = {"strategy_id": cfg.get("strategy_id", ""),
                       "period": cfg.get("period", ""), "config": cfg,
                       "report_path": str(path)}
            from app import db
            db.create_task(tid, task_name, "backtest", payload)
            db.save_report(tid, str(path))
            db.update_task(tid, status="success", progress=100, message="")
            r["task_id"] = tid
            print(f"  → 落库 {tid}", flush=True)
        done[combo] = r
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  → {combo}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    cfg0 = base_gate_on()
    for refill in (1, 2, 3):
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            cfg = mk_dyn(cfg0, oos, auto=True, top=50, refill=refill)
            run_and_log_save(cfg, f"COMBOR{refill}_{tag}",
                             f"组合-TOP50+换血线{refill}-{tag}(分钟)")

    _report(done)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(done: dict) -> None:
    lines = [
        "# 组合验证：TOP50 × 换血线 1/2/3（universe_auto=on + auto_top_x=50 + pool_refill_min∈{1,2,3}）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 对照三方：TOP50包(refill0)｜换血线单档(top30)｜off基座",
        "",
        "| 配置 | 段 | score | 超额 | 回撤 |",
        "|---|---|---|---|---|",
    ]
    for refill in (1, 2, 3):
        rf = done.get(f"COMBOR{refill}_全区间")
        ro = done.get(f"COMBOR{refill}_OOS段")
        if rf:
            lines.append(f"| **组合 TOP50+换血线{refill}** | full | {rf['score']:.4f} "
                         f"| {_pct(rf.get('excess_return'))} | {_pct(rf.get('max_drawdown'))} |")
        if ro:
            lines.append(f"| **组合 TOP50+换血线{refill}** | oos | {ro['score']:.4f} "
                         f"| {_pct(ro.get('excess_return'))} | {_pct(ro.get('max_drawdown'))} |")
    for name, seg in REFS.items():
        for tag in ("full", "oos"):
            r = seg[tag]
            lines.append(f"| {name} | {tag} | {r['score']:.4f} "
                         f"| {_pct(r.get('excess_return'))} | - |")
    lines += [
        "",
        "## 判定参考",
        "",
        "- 组合 ≥ TOP50包 → 换血与宽池兼容，可考虑纳入采纳包；",
        "- 组合 < TOP50包 → 维持纯 TOP50 包（refill=0），组合证伪。",
    ]
    out = OUT_DIR / f"pulse_dynsel_combo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
