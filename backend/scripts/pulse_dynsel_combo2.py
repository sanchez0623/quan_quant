# -*- coding: utf-8 -*-
"""组合补齐：换血线2 × 其他动态选股参数（idle 1/2/3、top 20）。

已测：换血线2档（top30+idle5+refill2）｜TOP50+换血线2（top50+idle5+refill2）。
本轮补：idle 1/2/3 × refill2（top30）、top20 × refill2（idle5）。8 回测，落库一体。
对照：REFILL2 档（=换血线2档，off 基座位级相同）｜off 基座。
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

from pulse_dynsel_oat import ROWS_JSONL as OAT_ROWS, mk_dyn  # noqa: E402
from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_dynsel_combo2_rows.jsonl"

# (任务描述, mk_dyn 参数, combo 前缀)
JOBS = [
    ("空仓触发1+换血线2", dict(idle=1, refill=2, top=30), "IDLE1R2"),
    ("空仓触发2+换血线2", dict(idle=2, refill=2, top=30), "IDLE2R2"),
    ("空仓触发3+换血线2", dict(idle=3, refill=2, top=30), "IDLE3R2"),
    ("池子20+换血线2", dict(idle=5, refill=2, top=20), "TOP20R2"),
]


def load_oat() -> dict:
    oat = {}
    if OAT_ROWS.exists():
        for line in OAT_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                oat[r["combo"]] = r
            except Exception:
                continue
    return oat


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
        print(f"续跑：已有 {len(done)} 条结果", flush=True)
    oat = load_oat()

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
    for desc, kw, pfx in JOBS:
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            cfg = mk_dyn(cfg0, oos, auto=True, **kw)
            cfg["name"] = f"combo2_{pfx}_{tag}"
            run_and_log_save(cfg, f"{pfx}_{tag}", f"{desc}-{tag}(分钟)")

    _report(done, oat)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(done: dict, oat: dict) -> None:
    ref = oat.get("REFILL2_全区间")   # 换血线2档（top30+idle5+refill2）=off 基座位级
    refo = oat.get("REFILL2_OOS段")
    off = oat.get("OFF_全区间")
    offo = oat.get("OFF_OOS段")
    lines = [
        "# 组合补齐：换血线2 × 其他动态选股参数（idle 1/2/3、top 20）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 换血线2档（top30+idle5+refill2）与 off 基座位级相同（历史结论）",
        "",
        "| 配置 | 段 | score | 超额 | 回撤 |",
        "|---|---|---|---|---|",
    ]
    if ref:
        lines.append(f"| 换血线2档(top30+idle5,参照) | full | {ref['score']:.4f} "
                     f"| {_pct(ref.get('excess_return'))} | {_pct(ref.get('max_drawdown'))} |")
    if refo:
        lines.append(f"| 换血线2档(top30+idle5,参照) | oos | {refo['score']:.4f} "
                     f"| {_pct(refo.get('excess_return'))} | {_pct(refo.get('max_drawdown'))} |")
    for desc, kw, pfx in JOBS:
        rf = done.get(f"{pfx}_全区间")
        ro = done.get(f"{pfx}_OOS段")
        if rf:
            lines.append(f"| {desc} | full | {rf['score']:.4f} "
                         f"| {_pct(rf.get('excess_return'))} | {_pct(rf.get('max_drawdown'))} |")
        if ro:
            lines.append(f"| {desc} | oos | {ro['score']:.4f} "
                         f"| {_pct(ro.get('excess_return'))} | {_pct(ro.get('max_drawdown'))} |")
    if off:
        lines.append(f"| off基座(对照) | full | {off['score']:.4f} "
                     f"| {_pct(off.get('excess_return'))} | {_pct(off.get('max_drawdown'))} |")
    if offo:
        lines.append(f"| off基座(对照) | oos | {offo['score']:.4f} "
                     f"| {_pct(offo.get('excess_return'))} | {_pct(offo.get('max_drawdown'))} |")
    lines += [
        "",
        "## 判定参考",
        "",
        "- 组合 vs 换血线2档：idle 缩短让重选更频繁（与换血叠加放大动作）；",
        "- top20 候选不足（单档已双崩），叠加换血预计进一步恶化。",
    ]
    out = OUT_DIR / f"pulse_dynsel_combo2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
