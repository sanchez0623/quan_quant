# -*- coding: utf-8 -*-
"""AB 复验：动态选股 TOP50（用户拍板：独立重跑双段确认位级后采纳）。

独立重跑 语境基线 TOP30 与 TOP50 的双段（非缓存），与 OAT 缓存位级对齐校验
（防缓存/传参事故）。位级一致 → 采纳：OAT 的 TOP50 两任务改名加 🏷️。
注意：auto_top_x=50 依赖 universe_auto=on，采纳的是语境包（on + top50 + idle5 + refill0）。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_dynsel_oat import ROWS_JSONL as OAT_ROWS, mk_dyn  # noqa: E402
from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"


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
    oat = load_oat()

    cfg0 = base_gate_on()
    rows = []  # (name, tag, r, oat_combo)

    def run_ab(name: str, top: int, oos: bool, oat_combo: str):
        tag = "oos" if oos else "full"
        cfg = mk_dyn(cfg0, oos, auto=True, top=top)
        cfg["name"] = f"ab_dynsel_top{top}_{tag}"
        from app.engine import runner
        rep = runner.run_backtest(cfg)
        s = _score(rep)
        m = rep.get("metrics", {}) or {}
        r = {"score": s["score"], "total_return": m.get("total_return"),
             "excess_return": m.get("excess_return"), "max_drawdown": m.get("max_drawdown")}
        rows.append((name, tag, r, oat_combo))
        print(f"[AB] {name} {tag}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}", flush=True)

    run_ab("语境基线TOP30", 30, False, "BASE_全区间")
    run_ab("语境基线TOP30", 30, True, "BASE_OOS段")
    run_ab("TOP50", 50, False, "TOP50_全区间")
    run_ab("TOP50", 50, True, "TOP50_OOS段")

    _report(rows, oat)
    _adopt_if_aligned(rows, oat)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _align(r: dict, oat_r: dict | None) -> str:
    if not oat_r:
        return "OAT缺"
    same = abs(r["score"] - oat_r["score"]) < 1e-6 and \
        abs((r.get("excess_return") or 0) - (oat_r.get("excess_return") or 0)) < 1e-6
    return "✓位级一致" if same else f"✗偏差 OAT={oat_r['score']:.4f}"


def _report(rows, oat: dict) -> None:
    lines = [
        "# AB 复验：动态选股 TOP50（语境包 on+top50+idle5+refill0+试仓20+闸门MA30）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 用户拍板：独立重跑双段确认位级后采纳（年度仲裁 2/4 已知，选择 AB 路径）",
        "",
        "| 项 | 段 | score | 超额 | 回撤 | OAT 对齐 |",
        "|---|---|---|---|---|---|",
    ]
    for name, tag, r, oat_combo in rows:
        lines.append(
            f"| {name} | {tag} | {r['score']:.4f} | {_pct(r.get('excess_return'))} "
            f"| {_pct(r.get('max_drawdown'))} | {_align(r, oat.get(oat_combo))} |")
    lines += [
        "",
        "## 判定",
        "",
        "- 4/4 位级一致 → 采纳语境包（universe_auto=on + auto_top_x=50）；"
        "载体任务改名打 🏷️。",
    ]
    out = OUT_DIR / f"pulse_dynsel_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _adopt_if_aligned(rows, oat: dict) -> None:
    aligned = all(_align(r, oat.get(oat_combo)) == "✓位级一致"
                  for name, tag, r, oat_combo in rows if name == "TOP50")
    if not aligned:
        print("AB 复验位级不一致，不采纳", flush=True)
        return
    old_names = ("池子大小50-全区间(分钟)", "池子大小50-OOS段(分钟)")
    conn = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
    for old in old_names:
        row = conn.execute("SELECT id FROM tasks WHERE name=?", (old,)).fetchone()
        if not row:
            print(f"[改名跳过] 任务不存在：{old}", flush=True)
            continue
        from app import db
        new_name = old.replace("池子大小", "🏷️动态选股采纳TOP50-池", 1)
        db.update_task(row[0], name=new_name)
        print(f"[🏷️] {row[0]}  {old} -> {new_name}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
