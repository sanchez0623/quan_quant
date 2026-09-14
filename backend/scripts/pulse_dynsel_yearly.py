# -*- coding: utf-8 -*-
"""池子大小 TOP50 年度窗口仲裁（仿闸门 MA30 先例，用户拍板方案A）。

对照 = 动态选股语境基线（on + idle5 + top30 + refill0 + 试仓20 + 闸门MA30）。
候选 = 同配置 + auto_top_x=50。
8 回测 = 4 年度 × 2 形态，落库一体（防重 by 任务名）。
判定：TOP50 年度胜率 ≥3/4 且 Δ超额均值 > 0 → 稳健性确认（结合 OOS +43.4pt 证据采纳）；
否则 score 纪律维持，auto_top_x 保持 30。
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
from pulse_fwdt import _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_dynsel_year_rows.jsonl"
WINDOWS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
]
MKS = [("基线30", 30), ("TOP50", 50)]


def mk_year(cfg: dict, top: int, win_tag: str, s: str, e: str) -> dict:
    out = mk_dyn(cfg, False, True, top=top)
    out["start_date"] = s
    out["end_date"] = e
    out["name"] = f"dynsel_year_top{top}_{win_tag}"
    return out


def save_report_task(name: str, cfg: dict, rep: dict) -> str:
    from app import db
    reports = Path(__file__).resolve().parents[2] / "data" / "reports"
    reports.mkdir(exist_ok=True)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = reports / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, name, "backtest", payload)
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    return tid


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
            tid = save_report_task(task_name, cfg, rep)
            r["task_id"] = tid
            print(f"  → 落库 {tid}", flush=True)
        done[combo] = r
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  → {combo}: 超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    def _pct_local(v):
        return _pct(v)

    cfg0 = base_gate_on()
    for win_tag, s, e in WINDOWS:
        for label, top in MKS:
            combo = f"{label}_{win_tag}"
            run_and_log_save(mk_year(cfg0, top, win_tag, s, e), combo,
                             f"动态选股年度-{label}-{win_tag}(分钟)")

    rows = []
    wins = 0
    ds_all = []
    for win_tag, _s, _e in WINDOWS:
        b = done.get(f"基线30_{win_tag}")
        c = done.get(f"TOP50_{win_tag}")
        be = b.get("excess_return") if b else None
        ce = c.get("excess_return") if c else None
        d = (ce - be) if (ce is not None and be is not None) else None
        if d is not None:
            ds_all.append(d)
            wins += 1 if d > 0 else 0
        rows.append((win_tag, be, ce, d))
    _report(rows, wins, ds_all)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(rows, wins: int, ds_all: list) -> None:
    lines = [
        "# 池子大小 TOP50 年度窗口仲裁（动态选股语境，vs TOP30 基线，2022-2025 分年）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 基线 = 动态选股语境基线（on+idle5+top30+refill0+试仓20+闸门MA30）"
        "｜判定：胜率 ≥3/4 且 Δ均值>0 → 稳健（结合 OOS +43.4pt 采纳）",
        "",
        "| 年度 | TOP30超额 | TOP50超额 | Δ |",
        "|---|---|---|---|",
    ]
    for win_tag, be, ce, d in rows:
        lines.append(f"| {win_tag} | {_pct(be)} | {_pct(ce)} | {_pct(d)} |")
    m = sum(ds_all) / len(ds_all) if ds_all else 0
    lines += [
        "",
        f"- **TOP50**：年度胜率 {wins}/{len(ds_all)}｜Δ超额均值 {m:+.2%}",
        "",
        "## 判定",
        "",
        "- 胜率 ≥3/4 且均值>0 → 年度稳健性确认（结合 OOS +43.4pt 采纳 auto_top_x=50）；",
        "- 否则 score 纪律优先，维持 top_x=30（动态选股语境整体搁置另议）。",
    ]
    out = OUT_DIR / f"pulse_dynsel_yearly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
