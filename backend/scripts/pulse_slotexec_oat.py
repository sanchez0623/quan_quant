# -*- coding: utf-8 -*-
"""执行层 6 参数组 P0 OAT 扫描 + 落库一体（基座=采纳形态闸门MA30，bud25 缓存为基线）。

组：试仓占比 base_pct_min(10) / 满配占比 base_pct_max(50) / 最大加仓次数 max_adds(2)
    / 加仓递减 add_scale(0.5) / 加仓冷却 add_cooldown(5) / 新高窗口 add_breakout_n(20)
档位=默认值时与基线位级相同 → 跳跑，报告标注"默认等效"（基线已有载体不重复落库）。
采纳线（P0 纪律）：Δscore > 0.01 且 OOS 超额 ≥ 基线。
落库一体化：run_backtest 的 rep 在手直接写 reports/ + tasks 表（防重 by 任务名）。
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
from pulse_fwdtbud_oat import ROWS_JSONL as BUD_ROWS  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_gatema_oat import mk  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_slotexec_rows.jsonl"

# (中文名, params键, 用户档位, 默认值)
GROUPS = [
    ("试仓占比", "base_pct_min", [5, 10, 15, 20], 10),
    ("满配占比", "base_pct_max", [10, 20, 30], 50),
    ("最大加仓次数", "max_adds", [3, 4, 6, 8, 10], 2),
    ("加仓递减", "add_scale", [0.3, 0.5, 0.75, 1], 0.5),
    ("加仓冷却", "add_cooldown", [3, 5, 8, 10, 15], 5),
    ("新高窗口", "add_breakout_n", [3, 5, 8, 10, 12], 20),
]


def mk_p(cfg: dict, key: str, val, oos: bool) -> dict:
    out = mk(cfg, 30, oos)  # 采纳形态：闸门 MA30 + 双段
    out["params"][key] = val  # 策略参数必须写 params 子字典
    out["name"] = f"slotexec_{key}_{val}_{'oos' if oos else 'full'}"
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


def load_baseline() -> dict:
    base = {}
    if BUD_ROWS.exists():
        for line in BUD_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("bud25_full", "bud25_oos"):
                    base[r["combo"]] = r
            except Exception:
                continue
    if "bud25_full" not in base or "bud25_oos" not in base:
        raise RuntimeError("pulse_fwdtbud_rows.jsonl 缺基线 bud25_full/bud25_oos")
    return base


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
        rep = runner_run(cfg)
        s = _score(rep)
        m = rep.get("metrics", {}) or {}
        r = {"combo": combo, "score": s["score"], "total_return": m.get("total_return"),
             "excess_return": m.get("excess_return"), "max_drawdown": m.get("max_drawdown")}
        if task_name not in task_names:
            tid = save_report_task(task_name, cfg, rep)
            r["task_id"] = tid
            print(f"  → 落库 {tid}", flush=True)
        else:
            print(f"  → 任务已存在，跳过落库：{task_name}", flush=True)
        done[combo] = r
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  → {combo}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    base = load_baseline()
    bf, bo = base["bud25_full"], base["bud25_oos"]
    boos_ex = bo.get("excess_return") or 0
    print(f"基线(默认档)：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}"
          f"｜OOS 超额 {_pct(boos_ex)}", flush=True)

    cfg0 = base_gate_on()
    for label, key, values, default in GROUPS:
        for val in values:
            if val == default:
                continue  # 默认等效档：与基线位级相同，跳跑
            for oos, tag in ((False, "全区间"), (True, "OOS段")):
                combo = f"{key}={val}_{tag}"
                task_name = f"{label}{val}-{tag}(分钟)"
                run_and_log_save(mk_p(cfg0, key, val, oos), combo, task_name)

    _report(done, bf, boos_ex)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def runner_run(cfg: dict) -> dict:
    from app.engine import runner
    return runner.run_backtest(cfg)


def _fmt_val(v) -> str:
    return f"{v:g}"


def _report(done: dict, bf: dict, boos_ex: float) -> None:
    lines = [
        "# 执行层 6 参数组 P0 OAT（基座=采纳形态闸门MA30）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 基线（各键默认档）：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}"
        f"/回撤 {_pct(bf.get('max_drawdown'))}｜OOS 超额 {_pct(boos_ex)}",
        "- 采纳线：Δscore > 0.01 且 OOS 超额 ≥ 基线｜默认等效档标注 =(基线)",
        "",
        "| 组 | 档 | 段 | score | Δscore | 超额 | OOS Δ超额 | 回撤 | 任务 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    passed = []
    for label, key, values, default in GROUPS:
        for val in values:
            if val == default:
                lines.append(f"| {label} | {_fmt_val(val)} | = | {bf['score']:.4f} | = | "
                             f"{_pct(bf.get('excess_return'))} | = | {_pct(bf.get('max_drawdown'))} "
                             f"| 基线载体 |")
                continue
            rf = done.get(f"{key}={val}_全区间")
            ro = done.get(f"{key}={val}_OOS段")
            if not (rf and ro):
                continue
            ds = rf["score"] - bf["score"]
            dx = (ro.get("excess_return") or 0) - boos_ex
            tid = rf.get("task_id", "")
            lines.append(
                f"| {label} | {_fmt_val(val)} | full | {rf['score']:.4f} | {ds:+.4f} "
                f"| {_pct(rf.get('excess_return'))} |  | {_pct(rf.get('max_drawdown'))} | {tid} |")
            lines.append(
                f"| {label} | {_fmt_val(val)} | oos | {ro['score']:.4f} |  "
                f"| {_pct(ro.get('excess_return'))} | {dx:+.2%} | {_pct(ro.get('max_drawdown'))} "
                f"| {ro.get('task_id', '')} |")
            if ds > 0.01 and dx >= 0:
                passed.append((label, val, ds, dx))
    lines += ["", "## 判定", ""]
    if passed:
        for label, val, ds, dx in passed:
            lines.append(f"- **{label}={_fmt_val(val)} 过线**（Δscore {ds:+.4f}，"
                         f"OOS Δ超额 {dx:+.2%}）→ 过线候选，进 AB 双段验证")
    else:
        lines.append("- 无档过线（Δscore>0.01 且 OOS 超额≥基线）→ 全组维持默认值")
    out = OUT_DIR / f"pulse_slotexec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
