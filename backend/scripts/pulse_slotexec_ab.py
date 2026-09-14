# -*- coding: utf-8 -*-
"""AB 复验：试仓占比 15/20（OAT 过线候选）独立重跑双段对照。

复验 = 独立重跑（非缓存）确认与 OAT 位级对齐（防缓存/传参事故）+ OOS 采纳线判定。
过线 → 按证据最强档采纳：把对应 OAT 任务改名加 🏷️（update_task）。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_fwdtbud_oat import ROWS_JSONL as BUD_ROWS  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_gatema_oat import mk  # noqa: E402
from pulse_slotexec_oat import ROWS_JSONL as OAT_ROWS, mk_p  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
AB_ITEMS = [15, 20]  # base_pct_min 过线档
ADOPT_VAL = 20  # 同参数互斥档位二选一：证据最强档（Δscore/OOS 均优于 15）


def load_oat() -> dict:
    oat = {}
    for path in (BUD_ROWS, OAT_ROWS):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
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
    bf_oat, bo_oat = oat["bud25_full"], oat["bud25_oos"]
    boos_ex = bo_oat.get("excess_return") or 0
    print(f"OAT 基线：full score {bf_oat['score']:.4f}/超额 {_pct(bf_oat.get('excess_return'))}"
          f"｜OOS 超额 {_pct(boos_ex)}", flush=True)

    cfg0 = base_gate_on()
    rows = []  # (name, tag, r, oat_combo)

    def run_ab(name: str, key: str | None, val, oos: bool, oat_combo: str):
        tag = "oos" if oos else "full"
        if key:
            cfg = mk_p(cfg0, key, val, oos)
        else:
            cfg = mk(cfg0, 30, oos)  # 基座=采纳形态默认档
            cfg["name"] = f"ab_base_{tag}"
        r = runner_rs(cfg)
        rows.append((name, tag, r, oat_combo))
        print(f"[AB] {name} {tag}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}", flush=True)

    def runner_rs(cfg: dict) -> dict:
        from app.engine import runner
        rep = runner.run_backtest(cfg)
        s = _score(rep)
        m = rep.get("metrics", {}) or {}
        s["total_return"] = m.get("total_return")
        s["excess_return"] = m.get("excess_return")
        s["max_drawdown"] = m.get("max_drawdown")
        return s

    run_ab("基座(默认10)", None, None, False, "bud25_full")
    run_ab("基座(默认10)", None, None, True, "bud25_oos")
    for val in AB_ITEMS:
        run_ab(f"试仓占比{val}", "base_pct_min", val, False, f"base_pct_min={val}_全区间")
        run_ab(f"试仓占比{val}", "base_pct_min", val, True, f"base_pct_min={val}_OOS段")

    _report(rows, oat, boos_ex)
    _adopt_if_pass(rows, boos_ex, oat)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _align(r: dict, oat_r: dict | None) -> str:
    if not oat_r:
        return "OAT缺"
    same = abs(r["score"] - oat_r["score"]) < 1e-6 and \
        abs((r.get("excess_return") or 0) - (oat_r.get("excess_return") or 0)) < 1e-6
    return "✓位级一致" if same else f"✗偏差 OAT={oat_r['score']:.4f}"


def _report(rows, oat: dict, boos_ex: float) -> None:
    lines = [
        "# AB 复验：试仓占比 15/20（基座=采纳形态闸门MA30，独立重跑）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 复验：独立重跑对照 OAT 缓存（位级一致性校验，防缓存/传参事故）",
        "",
        "| 项 | 段 | score | 超额 | OOS Δ超额 | OAT 对齐 |",
        "|---|---|---|---|---|---|",
    ]
    base_oos_ex = None
    for name, tag, r, oat_combo in rows:
        if name == "基座(默认10)" and tag == "oos":
            base_oos_ex = r.get("excess_return") or 0
    for name, tag, r, oat_combo in rows:
        dx = ""
        if tag == "oos":
            dx = f"{(r.get('excess_return') or 0) - boos_ex:+.2%}" if name != "基座(默认10)" \
                else "(基线)"
        lines.append(
            f"| {name} | {tag} | {r['score']:.4f} | {_pct(r.get('excess_return'))} "
            f"| {dx} | {_align(r, oat.get(oat_combo))} |")
    lines += ["", "## 判定", "",
              f"- 基座 OOS 超额 {_pct(base_oos_ex)}｜采纳线：OOS Δ超额 ≥ 0 且 score 复验一致"]
    out = OUT_DIR / f"pulse_slotexec_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _adopt_if_pass(rows, boos_ex: float, oat: dict) -> None:
    ok = {}
    for name, tag, r, oat_combo in rows:
        if name.startswith("试仓占比") and tag == "oos":
            val = int(name.replace("试仓占比", ""))
            ok[val] = (r.get("excess_return") or 0) >= boos_ex and \
                _align(r, oat.get(oat_combo)) == "✓位级一致"
    val = ADOPT_VAL
    if not ok.get(val):
        print(f"AB 未过线或复验不一致（{ok}），不采纳", flush=True)
        return
    old_names = (f"试仓占比{val}-全区间(分钟)", f"试仓占比{val}-OOS段(分钟)")
    conn = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
    for old in old_names:
        row = conn.execute("SELECT id FROM tasks WHERE name=?", (old,)).fetchone()
        if not row:
            print(f"[改名跳过] 任务不存在：{old}", flush=True)
            continue
        tid = row[0]
        new_name = old.replace("试仓占比", "🏷️试仓占比采纳", 1)
        from app import db
        db.update_task(tid, name=new_name)
        print(f"[🏷️] {old} → {new_name}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
