# -*- coding: utf-8 -*-
"""ATR 移动止损参数扫描（基座=bt_76889c798212，k1=2.5/k2=6.0）。

止损线 = max(成本项 − k1×ATR, 最高价 − k2×ATR)（adaptive=trend 缩放，棘轮关，成本基准 first）。
K1 = atr_multiplier（硬止损兜底）：{2.0, 3.0, 3.5}（紧/松两侧）
K2 = atr_trail_mult（移动锁盈）：{7.5, 9}（放宽侧；收紧侧 3/4.5 已在 P2 多轮全灭不重测）
5 档 × 双段 = 10 回测。基线复用 pulse_debt_rows.jsonl（D1 双段，同基座）。
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
ROWS_JSONL = OUT_DIR / "pulse_atr_rows.jsonl"
DEBT_ROWS = OUT_DIR / "pulse_debt_rows.jsonl"
# (标签, risk键, 值)
GRID = [
    ("K1=2.0", "atr_multiplier", 2.0),
    ("K1=3.0", "atr_multiplier", 3.0),
    ("K1=3.5", "atr_multiplier", 3.5),
    ("K2=7.5", "atr_trail_mult", 7.5),
    ("K2=9", "atr_trail_mult", 9),
]


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
    if DEBT_ROWS.exists():
        for line in DEBT_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("D1_full", "D1_oos"):
                    base[r["combo"]] = r
            except Exception:
                continue
    if not base:
        raise RuntimeError("pulse_debt_rows.jsonl 缺基线（D1_full/D1_oos）")
    bf, bo = base["D1_full"], base["D1_oos"]
    boos_ex = bo.get("excess_return") or 0
    print(f"基线(k1=2.5/k2=6.0)：全区间 score {bf['score']:.4f}/"
          f"超额 {_pct(bf.get('excess_return'))}｜OOS 超额 {_pct(boos_ex)}", flush=True)

    if args.adopt:
        best = None
        for tag, key, val in GRID:
            r, o = done.get(f"{tag}_full"), done.get(f"{tag}_oos")
            if r and o and (o.get("excess_return") or -9) >= boos_ex \
                    and r["score"] > bf["score"]:
                if best is None or r["score"] > best[1]["score"]:
                    best = (tag, key, val, r)
        if not best:
            print("无可采纳档位，维持 k1=2.5/k2=6.0", flush=True)
            return
        tag, key, val, _r = best
        cfg0 = base_gate_on()
        full = json.loads(json.dumps(cfg0))
        full["risk_config"][key] = val
        full["name"] = f"atr_adopt_{tag}_full"
        oos = json.loads(json.dumps(cfg0))
        oos["risk_config"][key] = val
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"atr_adopt_{tag}_oos"
        _save(f"🏷️ATR止损采纳-{tag}-全区间(分钟)", full)
        _save(f"🏷️ATR止损采纳-{tag}-OOS段(分钟)", oos)
        return

    cfg0 = base_gate_on()
    for tag, key, val in GRID:
        full = json.loads(json.dumps(cfg0))
        full["risk_config"][key] = val
        full["name"] = f"atr_{tag}_full"
        run_and_log(full, f"{tag}_full", f"{tag} 全区间")
        oos = json.loads(json.dumps(cfg0))
        oos["risk_config"][key] = val
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"atr_{tag}_oos"
        run_and_log(oos, f"{tag}_oos", f"{tag} OOS")
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
        "# ATR 移动止损参数扫描（k1 硬止损 / k2 移动锁盈）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_76889c798212 形态（k1=2.5/k2=6.0，adaptive=trend，棘轮关，成本 first）",
        f"- 基线：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}｜"
        f"OOS 超额 {_pct(boos_ex)}",
        "- K2 收紧侧（3/4.5）已在 P2 多轮全灭，本轮只测放宽侧",
        "",
        "| 档 | 段 | score | 总收益 | 超额 | 回撤 | Δscore | OOS Δ超额 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tag, _key, _val in GRID:
        for seg in ("full", "oos"):
            r = done.get(f"{tag}_{seg}")
            if not r:
                continue
            dex = ""
            if seg == "oos":
                dex = f"{(r.get('excess_return') or 0)-boos_ex:+.2%}"
            lines.append(
                f"| {tag} | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
                f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} "
                f"| {r['score']-bf['score']:+.4f} | {dex} |")
    out = OUT_DIR / f"pulse_atr_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
