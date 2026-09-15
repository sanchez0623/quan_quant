# -*- coding: utf-8 -*-
"""因子扩展 P0 OAT（FACTOR_EXT，基座=采纳形态10项含动态选股TOP50包）。

实验1（方案B）：lagged_vol_tstat 禁入过滤（lagvol_filter=on）× lagvol_z_max ∈ {1.5,2,2.5,3}
实验2（方案A）：shadow_z 衰退第四票（shadow_exit=on）× shadow_z_exit ∈ {1,1.5,2}
参数走 params 子字典（策略参数）。基线 = TOP50 语境包双段（缓存/载体已有）。
采纳线（P0）：Δscore > 0.01 且 OOS 超额 ≥ 基线。落库一体（防重 by 任务名）。
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

from pulse_dynsel_oat import ROWS_JSONL as DYN_ROWS, mk_dyn  # noqa: E402
from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_factorext_rows.jsonl"

EXPERIMENTS = [
    ("lagvol禁入", "lagvol", [("lagvol_filter", "on", "lagvol_z_max", 1.5),
                              ("lagvol_filter", "on", "lagvol_z_max", 2.0),
                              ("lagvol_filter", "on", "lagvol_z_max", 2.5),
                              ("lagvol_filter", "on", "lagvol_z_max", 3.0)]),
    ("影线衰退票", "shadow", [("shadow_exit", "on", "shadow_z_exit", 1.0),
                              ("shadow_exit", "on", "shadow_z_exit", 1.5),
                              ("shadow_exit", "on", "shadow_z_exit", 2.0)]),
]


def mk_fe(cfg: dict, oos: bool, overrides: dict) -> dict:
    out = mk_dyn(cfg, oos, auto=True, top=50)  # 采纳形态：TOP50 语境包
    out["params"].update(overrides)  # 策略参数必须写 params 子字典
    tag = "_".join(f"{k.replace('_filter', '').replace('_exit', '').replace('_z_max', '')}"
                   f"{v:g}" if isinstance(v, float) else f"{k[-4:]}{v}"
                   for k, v in overrides.items())
    out["name"] = f"factorext_{tag}_{'oos' if oos else 'full'}"
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

    # 基线：TOP50 双段缓存（10 项采纳形态载体）
    base = {}
    if DYN_ROWS.exists():
        for line in DYN_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("TOP50_全区间", "TOP50_OOS段"):
                    base[r["combo"]] = r
            except Exception:
                continue
    if "TOP50_全区间" not in base or "TOP50_OOS段" not in base:
        raise RuntimeError("pulse_dynsel_rows.jsonl 缺 TOP50 双段缓存")
    bf = base["TOP50_全区间"]
    bo = base["TOP50_OOS段"]
    boos_ex = bo.get("excess_return") or 0
    print(f"基线(10项采纳形态)：full score {bf['score']:.4f}/"
          f"超额 {_pct(bf.get('excess_return'))}｜OOS 超额 {_pct(boos_ex)}", flush=True)

    cfg0 = base_gate_on()
    for label, _pfx, variants in EXPERIMENTS:
        for overrides in variants:
            keys = (overrides[0], overrides[2])
            tag = f"{keys[0][-4:]}{overrides[1]}_{keys[1][-5:]}{overrides[3]:g}"
            for oos, seg in ((False, "全区间"), (True, "OOS段")):
                combo = f"{tag}_{seg}"
                task_name = f"因子扩展-{label}-z{overrides[3]:g}-{seg}(分钟)"
                run_and_log_save(mk_fe(cfg0, oos,
                                       dict(zip((overrides[0], overrides[2]),
                                                (overrides[1], overrides[3])))),
                                 combo, task_name)

    _report(done, bf, bo, boos_ex)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(done: dict, bf: dict, bo: dict, boos_ex: float) -> None:
    lines = [
        "# 因子扩展 P0 OAT（FACTOR_EXT，基座=采纳形态10项含动态选股TOP50包）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 基线(2w/默认/因子关)：full score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}"
        f"/回撤 {_pct(bf.get('max_drawdown'))}｜OOS score {bo['score']:.4f}/超额 {_pct(boos_ex)}",
        "- 采纳线：Δscore > 0.01 且 OOS 超额 ≥ 基线",
        "",
        "| 实验 | 档 | 段 | score | Δscore | 超额 | OOS Δ超额 | 回撤 | 任务 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    passed = []
    for label, _pfx, variants in EXPERIMENTS:
        for overrides in variants:
            keys = (overrides[0], overrides[2])
            tag = f"{keys[0][-4:]}{overrides[1]}_{keys[1][-5:]}{overrides[3]:g}"
            disp = "/".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                            for k, v in zip((overrides[0], overrides[2]),
                                            (overrides[1], overrides[3])))
            rf = done.get(f"{tag}_全区间")
            ro = done.get(f"{tag}_OOS段")
            if not (rf and ro):
                continue
            ds = rf["score"] - bf["score"]
            dx = (ro.get("excess_return") or 0) - boos_ex
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
    if passed:
        for label, disp, ds, dx in passed:
            lines.append(f"- **{label} {disp} 过线**（Δscore {ds:+.4f}，OOS Δ超额 {dx:+.2%}）"
                         f"→ 过线候选，进 AB 双段验证")
    else:
        lines.append("- 无档过线（Δscore>0.01 且 OOS 超额≥基线）→ 因子证伪，维持默认关闭")
    out = OUT_DIR / f"pulse_factorext_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
