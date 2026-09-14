# -*- coding: utf-8 -*-
"""正向T买入占比 fwd_t_budget_pct 扫描（P0 OAT，新语境=大盘闸门MA30 采纳形态）。

基线 = 采纳形态（bt_b97b590f8d4f：前六项 + index_gate=on/MA30，budget=25 默认档）。
档位 10/35/50 × 双段（全区间 + OOS）。
采纳线（P0 纪律）：Δscore > 0.01 且 OOS 超额 ≥ 基线 → 过线候选进 AB；否则证伪。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_gatema_oat import mk  # noqa: E402
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_fwdtbud_rows.jsonl"
GRID = [10, 35, 50]
BASE_BUDGET = 25


def mk_bud(cfg: dict, budget: int, oos: bool) -> dict:
    out = mk(cfg, 30, oos)  # 采纳形态：闸门 MA30 + 双段切分
    # 策略参数在 params 子字典（runner.py: apply_param_defaults(strategy_id, cfg["params"])），
    # 写顶层不生效（历史事故：budget=10 与 25 位级相同）
    out["params"]["fwd_t_budget_pct"] = budget
    out["name"] = f"fwdtbud_{budget}_{'oos' if oos else 'full'}"
    return out


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

    def run_and_log(cfg: dict, combo: str, desc: str) -> dict:
        if combo in done:
            print(f"[缓存] {desc}", flush=True)
            return done[combo]
        print(f"[回测] {desc} ...", flush=True)
        r = run_score(cfg)
        r["combo"] = combo
        done[combo] = r
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  → {desc}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    cfg0 = base_gate_on()
    base = {}
    for oos, tag in ((False, "full"), (True, "oos")):
        base[tag] = run_and_log(mk_bud(cfg0, BASE_BUDGET, oos), f"bud{BASE_BUDGET}_{tag}",
                                f"基线 budget={BASE_BUDGET} {'OOS' if oos else '全区间'}")
    bf = base["full"]
    boos_ex = base["oos"].get("excess_return") or 0
    print(f"基线(采纳形态闸门MA30)：全区间 score {bf['score']:.4f}/"
          f"超额 {_pct(bf.get('excess_return'))}｜OOS 超额 {_pct(boos_ex)}", flush=True)

    for budget in GRID:
        for oos, tag in ((False, "full"), (True, "oos")):
            run_and_log(mk_bud(cfg0, budget, oos), f"bud{budget}_{tag}",
                        f"budget={budget} {'OOS' if oos else '全区间'}")

    _report(done, bf, boos_ex)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(done: dict, bf: dict, boos_ex: float) -> None:
    lines = [
        "# 正向T买入占比 fwd_t_budget_pct 扫描（P0 OAT，基座=采纳形态闸门MA30）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 基线 budget=25：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}"
        f"/回撤 {_pct(bf.get('max_drawdown'))}｜OOS 超额 {_pct(boos_ex)}",
        "- 采纳线：Δscore > 0.01 且 OOS 超额 ≥ 基线",
        "",
        "| budget | 段 | score | Δscore | 总收益 | 超额 | OOS Δ超额 | 回撤 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    passed = []
    for budget in GRID:
        rf = done.get(f"bud{budget}_full")
        ro = done.get(f"bud{budget}_oos")
        if not (rf and ro):
            continue
        ds = rf["score"] - bf["score"]
        dx = (ro.get("excess_return") or 0) - boos_ex
        lines.append(
            f"| {budget} | full | {rf['score']:.4f} | {ds:+.4f} | {_pct(rf.get('total_return'))} "
            f"| {_pct(rf.get('excess_return'))} |  | {_pct(rf.get('max_drawdown'))} |")
        lines.append(
            f"| {budget} | oos | {ro['score']:.4f} |  | {_pct(ro.get('total_return'))} "
            f"| {_pct(ro.get('excess_return'))} | {dx:+.2%} | {_pct(ro.get('max_drawdown'))} |")
        if ds > 0.01 and dx >= 0:
            passed.append((budget, ds, dx))
    lines += ["", "## 判定", ""]
    if passed:
        for budget, ds, dx in passed:
            lines.append(f"- **budget={budget} 过线**（Δscore {ds:+.4f}，OOS Δ超额 {dx:+.2%}）"
                         f"→ 过线候选，进 AB 双段验证")
    else:
        lines.append("- 无档过线（Δscore>0.01 且 OOS 超额≥基线）→ 维持 budget=25")
    out = OUT_DIR / f"pulse_fwdtbud_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
