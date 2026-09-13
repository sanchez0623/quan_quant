# -*- coding: utf-8 -*-
"""池级趋势开关阈值扫描：pool_gate_enter_th 0.05-0.25（基座=bt_76889c798212，现值 0.15）。

语义：池内动量分>0 占比 < enter_th 连续 2 日 → gate 关（停开仓）；恢复线 = enter_th×2（滞回）。
档位 {0.05,0.10,0.20,0.25} × 双段 = 8 回测，0.15 基线复用 pulse_debt_rows（D1 双段）。
采纳线（P0 纪律）：OOS 超额 ≥ 基线 且全区间 score 正增量。
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_debt_oat import VOL_ROWS  # noqa: E402
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_gateth_rows.jsonl"
GRID = [0.05, 0.10, 0.20, 0.25]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adopt", action="store_true")
    args = ap.parse_args()
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

    base = {}
    if VOL_ROWS.exists():
        for line in VOL_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("BASE_full", "BASE_oos"):
                    base[r["combo"]] = r
            except Exception:
                continue
    debt = {}
    debt_rows = OUT_DIR / "pulse_debt_rows.jsonl"
    if debt_rows.exists():
        for line in debt_rows.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("D1_full", "D1_oos"):
                    debt[r["combo"]] = r
            except Exception:
                continue
    bf = debt.get("D1_full") or base.get("BASE_full")
    bo = debt.get("D1_oos") or base.get("BASE_oos")
    if not (bf and bo):
        raise RuntimeError("缺基线（D1_full/D1_oos）")
    boos_ex = bo.get("excess_return") or 0
    print(f"基线(enter_th=0.15)：全区间 score {bf['score']:.4f}/"
          f"超额 {_pct(bf.get('excess_return'))}｜OOS 超额 {_pct(boos_ex)}", flush=True)

    if args.adopt:
        best = None
        for th in GRID:
            r, o = done.get(f"TH{th}_full"), done.get(f"TH{th}_oos")
            if r and o and (o.get("excess_return") or -9) >= boos_ex \
                    and r["score"] > bf["score"]:
                if best is None or r["score"] > best[1]["score"]:
                    best = (th, r)
        if not best:
            print("无可采纳档位，维持 0.15", flush=True)
            return
        th = best[0]
        cfg0 = base_gate_on()
        full = json.loads(json.dumps(cfg0))
        full["pool_gate_enter_th"] = th
        full["name"] = f"gateth_{th}_full"
        oos = json.loads(json.dumps(cfg0))
        oos["pool_gate_enter_th"] = th
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"gateth_{th}_oos"
        _save(f"🏷️gate阈值采纳-{th}-全区间(分钟)", full)
        _save(f"🏷️gate阈值采纳-{th}-OOS段(分钟)", oos)
        return

    cfg0 = base_gate_on()
    for th in GRID:
        full = json.loads(json.dumps(cfg0))
        full["pool_gate_enter_th"] = th
        full["name"] = f"gateth_{th}_full"
        run_and_log(full, f"TH{th}_full", f"gate阈值{th} 全区间")
        oos = json.loads(json.dumps(cfg0))
        oos["pool_gate_enter_th"] = th
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"gateth_{th}_oos"
        run_and_log(oos, f"TH{th}_oos", f"gate阈值{th} OOS")
    _report(done, bf, boos_ex)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _save(name: str, cfg: dict) -> None:
    from app import db
    from app.engine import runner
    reports = Path(__file__).resolve().parents[2] / "data" / "reports"
    reports.mkdir(exist_ok=True)
    rep = runner.run_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = reports / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, name, "backtest", payload)
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    print(f"{tid}  {name}  收益 {m.get('total_return'):+.2%}  "
          f"超额 {m.get('excess_return'):+.2%}", flush=True)


def _report(done: dict, bf: dict, boos_ex: float) -> None:
    lines = [
        "# 池级趋势开关阈值扫描（pool_gate_enter_th 0.05-0.25，基座 0.15）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_76889c798212 形态｜恢复线 = 阈值×2（滞回联动）",
        f"- 基线(0.15)：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}｜"
        f"OOS 超额 {_pct(boos_ex)}",
        "",
        "| 阈值(恢复线) | 段 | score | 总收益 | 超额 | 回撤 | Δscore | OOS Δ超额 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for th in GRID:
        for seg in ("full", "oos"):
            r = done.get(f"TH{th}_{seg}")
            if not r:
                continue
            dex = ""
            if seg == "oos":
                dex = f"{(r.get('excess_return') or 0)-boos_ex:+.2%}"
            lines.append(
                f"| {th}({th*2:.2f}) | {seg} | {r['score']:.4f} "
                f"| {_pct(r.get('total_return'))} | {_pct(r.get('excess_return'))} "
                f"| {_pct(r.get('max_drawdown'))} | {r['score']-bf['score']:+.4f} | {dex} |")
    out = OUT_DIR / f"pulse_gateth_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
