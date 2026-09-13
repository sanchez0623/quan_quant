# -*- coding: utf-8 -*-
"""动量窗口参数扫描（基座=bt_76889c798212，mom_short=15/mom_mid=90——用户微调值，
引擎默认 short=10/mid=20）。选股层首扫：窗口变化会重构动态池。

mom_short：{10, 20}（短窗口敏感度两侧）
mom_mid：{60, 120}（用户拉长的 90 两侧；120 与 mom_long=120 重叠效应）
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
ROWS_JSONL = OUT_DIR / "pulse_mom_rows.jsonl"
DEBT_ROWS = OUT_DIR / "pulse_debt_rows.jsonl"
GRID = [
    ("SHORT=10", "mom_short", 10),
    ("SHORT=20", "mom_short", 20),
    ("MID=60", "mom_mid", 60),
    ("MID=120", "mom_mid", 120),
    ("SHORT=10+MID=60", None, None),  # 联合档：双回默认
]


def mk(cfg: dict, key, val, tag: str, oos: bool) -> dict:
    out = json.loads(json.dumps(cfg))
    if key is not None:
        out["params"][key] = val
    else:
        out["params"]["mom_short"] = 10
        out["params"]["mom_mid"] = 60
    if oos:
        out["start_date"] = OOS_SPLIT
    out["name"] = f"mom_{tag}_{'oos' if oos else 'full'}"
    return out


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
    print(f"基线(short=15/mid=90)：全区间 score {bf['score']:.4f}/"
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
            print("无可采纳档位，维持 short=15/mid=90", flush=True)
            return
        tag, key, val, _r = best
        cfg0 = base_gate_on()
        full = mk(cfg0, key, val, f"adopt_{tag}", False)
        oos = mk(cfg0, key, val, f"adopt_{tag}", True)
        _save(f"🏷️动量窗口采纳-{tag}-全区间(分钟)", full)
        _save(f"🏷️动量窗口采纳-{tag}-OOS段(分钟)", oos)
        return

    cfg0 = base_gate_on()
    for tag, key, val in GRID:
        run_and_log(mk(cfg0, key, val, tag, False), f"{tag}_full", f"{tag} 全区间")
        run_and_log(mk(cfg0, key, val, tag, True), f"{tag}_oos", f"{tag} OOS")
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
        "# 动量窗口参数扫描（mom_short / mom_mid，选股层首扫）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_76889c798212 形态（short=15/mid=90，用户微调；引擎默认 10/20）",
        f"- 基线：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}｜"
        f"OOS 超额 {_pct(boos_ex)}",
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
    out = OUT_DIR / f"pulse_mom_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
