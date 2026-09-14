# -*- coding: utf-8 -*-
"""动态选股（universe_auto）三参数组 P0 OAT + 落库一体（基座=采纳形态9项含试仓20）。

键（均 cfg 顶层，非 params 子字典）：
- universe_auto（动态选股开关，默认 False）
- auto_idle_days（空仓触发交易日，默认 5；用户档 1/2/3/5）
- pool_refill_min（枯竭换血线-持仓少于，默认 0=关闭；用户档 1/2/3 全为开启）
- auto_top_x（池子大小-预筛取前x只，默认 30；用户档 20/30/50）

结构：
- 基座 = 采纳形态 + base_pct_min=20，universe_auto=off（对照"动态选股关"）
- 语境基线 = on + idle5 + top30 + refill0（兼作组1的 5 档与组3的 30 档）
- 组1 idle=1/2/3｜组2 refill=1/2/3｜组3 top=20/50（30 由语境基线覆盖）
采纳线（P0）：Δscore > 0.01 且 OOS 超额 ≥ 语境基线（参数边际）；
另报"动态选股本身值不值"（语境基线 vs off 基座）。
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

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_gatema_oat import mk  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_dynsel_rows.jsonl"


def mk_dyn(cfg: dict, oos: bool, auto: bool, idle: int = 5, refill: int = 0,
           top: int = 30) -> dict:
    out = mk(cfg, 30, oos)  # 采纳形态闸门 MA30 + 双段
    out["params"]["base_pct_min"] = 20  # 第九项采纳（试仓资金占比20）
    if auto:
        out["universe_auto"] = True
        out["auto_idle_days"] = idle
        out["auto_top_x"] = top
        out["pool_refill_min"] = refill
    out["name"] = (f"dynsel_{'on' if auto else 'off'}_i{idle}_r{refill}_t{top}"
                   f"_{'oos' if oos else 'full'}")
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
        print(f"  → {combo}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    cfg0 = base_gate_on()
    # 基座：动态选股关（对照）
    for oos, tag in ((False, "全区间"), (True, "OOS段")):
        combo = f"OFF_{tag}"
        run_and_log_save(mk_dyn(cfg0, oos, auto=False), combo,
                         f"动态选股关基座-{tag}(分钟)")
    # 语境基线：on + 全默认（idle5/top30/refill0）
    for oos, tag in ((False, "全区间"), (True, "OOS段")):
        combo = f"BASE_{tag}"
        run_and_log_save(mk_dyn(cfg0, oos, auto=True), combo,
                         f"动态选股语境基线-{tag}(分钟)")

    bf = done["OFF_全区间"]
    bo = done["OFF_OOS段"]
    bfo_ex = bf.get("excess_return") or 0
    boos_ex = bo.get("excess_return") or 0
    mf = done["BASE_全区间"]
    mo = done["BASE_OOS段"]
    moos_ex = mo.get("excess_return") or 0
    print(f"基座(off)：full score {bf['score']:.4f}/超额 {_pct(bfo_ex)}"
          f"｜OOS 超额 {_pct(boos_ex)}", flush=True)
    print(f"语境基线(on默认)：full score {mf['score']:.4f}/超额 {_pct(mf.get('excess_return'))}"
          f"｜OOS 超额 {_pct(moos_ex)}", flush=True)

    # 三组参数档
    for idle in (1, 2, 3):  # 5=语境基线
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            run_and_log_save(mk_dyn(cfg0, oos, True, idle=idle), f"IDLE{idle}_{tag}",
                             f"空仓触发{idle}-{tag}(分钟)")
    for refill in (1, 2, 3):
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            run_and_log_save(mk_dyn(cfg0, oos, True, refill=refill), f"REFILL{refill}_{tag}",
                             f"枯竭换血线{refill}-{tag}(分钟)")
    for top in (20, 50):  # 30=语境基线
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            run_and_log_save(mk_dyn(cfg0, oos, True, top=top), f"TOP{top}_{tag}",
                             f"池子大小{top}-{tag}(分钟)")

    _report(bf, bo, mf, mo, done)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(bf: dict, bo: dict, mf: dict, mo: dict, done: dict) -> None:
    boos_ex = bo.get("excess_return") or 0
    moos_ex = mo.get("excess_return") or 0
    lines = [
        "# 动态选股（universe_auto）三参数组 P0 OAT（基座=采纳形态9项含试仓20）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 基座(off)：full score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}"
        f"｜OOS 超额 {_pct(boos_ex)}",
        f"- 语境基线(on默认)：full score {mf['score']:.4f}/超额 {_pct(mf.get('excess_return'))}"
        f"｜OOS 超额 {_pct(moos_ex)}",
        "- 采纳线：Δscore > 0.01 且 OOS 超额 ≥ 语境基线（参数边际）；默认等效档标注 =(基线)",
        "",
        "| 组 | 档 | 段 | score | Δscore | 超额 | OOS Δ超额 | 回撤 | 任务 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    passed = []
    groups = [
        ("空仓触发", "IDLE", [("1", "IDLE1"), ("2", "IDLE2"), ("3", "IDLE3"),
                              ("5", "BASE")], "5"),
        ("换血线", "REFILL", [("1", "REFILL1"), ("2", "REFILL2"), ("3", "REFILL3")], "0关"),
        ("池子大小", "TOP", [("20", "TOP20"), ("30", "BASE"), ("50", "TOP50")], "30"),
    ]
    for label, _pfx, items, default in groups:
        for disp, pfx in items:
            rf = done.get(f"{pfx}_全区间")
            ro = done.get(f"{pfx}_OOS段")
            if pfx == "BASE":
                lines.append(f"| {label} | {disp} | = | {mf['score']:.4f} | = | "
                             f"{_pct(mf.get('excess_return'))} | = | {_pct(mf.get('max_drawdown'))} "
                             f"| 语境基线载体 |")
                continue
            if not (rf and ro):
                continue
            ds = rf["score"] - mf["score"]
            dx = (ro.get("excess_return") or 0) - moos_ex
            lines.append(
                f"| {label} | {disp} | full | {rf['score']:.4f} | {ds:+.4f} "
                f"| {_pct(rf.get('excess_return'))} |  | {_pct(rf.get('max_drawdown'))} "
                f"| {rf.get('task_id', '')} |")
            lines.append(
                f"| {label} | {disp} | oos | {ro['score']:.4f} |  "
                f"| {_pct(ro.get('excess_return'))} | {dx:+.2%} | {_pct(ro.get('max_drawdown'))} "
                f"| {ro.get('task_id', '')} |")
            if ds > 0.01 and dx >= 0:
                passed.append((label, disp, ds, dx))
    lines += ["", "## 判定", ""]
    ds_switch = mf["score"] - bf["score"]
    dx_switch = moos_ex - boos_ex
    lines.append(f"- **动态选股本身**（on vs off）：Δscore {ds_switch:+.4f}｜"
                 f"OOS Δ超额 {dx_switch:+.2%}｜全区间超额 "
                 f"{_pct(mf.get('excess_return'))} vs {_pct(bf.get('excess_return'))}")
    if passed:
        for label, disp, ds, dx in passed:
            lines.append(f"- **{label}={disp} 过线**（Δscore {ds:+.4f}，OOS Δ超额 {dx:+.2%}）"
                         f"→ 过线候选，进 AB 双段验证")
    else:
        lines.append("- 无参数档过线（Δscore>0.01 且 OOS 超额≥语境基线）→ 维持默认")
    out = OUT_DIR / f"pulse_dynsel_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
