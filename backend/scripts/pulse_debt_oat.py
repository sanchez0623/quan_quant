# -*- coding: utf-8 -*-
"""做T债务时限扫描：t_debt_max_days 1-10 天值扫描（基座=bt_f70e4260e8b2 形态）。

参数语义：做T高抛建立债务，须 N 个交易日内买回，超时作废转正式减仓（runner.py L52）。
基座默认 3。档位 {1,2,5,7,10} × 双段（全区间+OOS）= 10 次回测。
基线复用 pulse_vol_rows.jsonl（同基座 BASE_full/BASE_oos）。
采纳线（P0 纪律）：OOS 超额 ≥ 基座 OOS 超额，且全区间 score 为正增量。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_vol_oat import base_trend  # noqa: E402
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_debt_rows.jsonl"
VOL_ROWS = OUT_DIR / "pulse_vol_rows.jsonl"
GRID = [1, 2, 5, 7, 10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adopt", action="store_true", help="最优档落库（带🏷️）")
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

    # 基线：从 pulse_vol_rows.jsonl 复用同基座双段
    base = {}
    if VOL_ROWS.exists():
        for line in VOL_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("BASE_full", "BASE_oos"):
                    base[r["combo"]] = r
            except Exception:
                continue
    if not base:
        raise RuntimeError("pulse_vol_rows.jsonl 缺基线（BASE_full/BASE_oos）")
    bf, bo = base["BASE_full"], base["BASE_oos"]
    boos_ex = bo.get("excess_return") or 0
    print(f"基线：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}｜"
          f"OOS 超额 {_pct(boos_ex)}", flush=True)

    if args.adopt:
        best = None
        for d in GRID:
            r = done.get(f"D{d}_full")
            o = done.get(f"D{d}_oos")
            if r and o and (o.get("excess_return") or -9) >= boos_ex and r["score"] > bf["score"]:
                if best is None or r["score"] > best[1]["score"]:
                    best = (d, r)
        if not best:
            print("无可采纳档位（无 OOS 过线且全区间正增量者），维持基座 3 天", flush=True)
            return
        d = best[0]
        cfg0 = base_trend()
        full = json.loads(json.dumps(cfg0))
        full["params"]["t_debt_max_days"] = d
        full["name"] = f"debt_adopt_{d}_full"
        oos = json.loads(json.dumps(cfg0))
        oos["params"]["t_debt_max_days"] = d
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"debt_adopt_{d}_oos"
        _save("🏷️债务时限采纳-t" + str(d) + "天-全区间(分钟)", full)
        _save("🏷️债务时限采纳-t" + str(d) + "天-OOS段(分钟)", oos)
        return

    cfg0 = base_trend()
    for d in GRID:
        full = json.loads(json.dumps(cfg0))
        full["params"]["t_debt_max_days"] = d
        full["name"] = f"debt_{d}_full"
        run_and_log(full, f"D{d}_full", f"债务时限{d}天 全区间")
        oos = json.loads(json.dumps(cfg0))
        oos["params"]["t_debt_max_days"] = d
        oos["start_date"] = OOS_SPLIT
        oos["name"] = f"debt_{d}_oos"
        run_and_log(oos, f"D{d}_oos", f"债务时限{d}天 OOS")
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
        "# 做T债务时限扫描（t_debt_max_days 1-10 天，基座默认 3）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_f70e4260e8b2 形态｜语义：债务超时作废转正式减仓（利润落袋不回补）",
        f"- 基线(3天)：全区间 score {bf['score']:.4f}/超额 {_pct(bf.get('excess_return'))}｜"
        f"OOS 超额 {_pct(boos_ex)}",
        "",
        "| 时限 | 段 | score | 总收益 | 超额 | 回撤 | Δscore | OOS采纳 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in GRID:
        for seg in ("full", "oos"):
            r = done.get(f"D{d}_{seg}")
            if not r:
                continue
            b = bf if seg == "full" else None
            dex = ""
            ok = ""
            if seg == "oos":
                dv = (r.get("excess_return") or 0) - boos_ex
                dex = f"{dv:+.2%}"
                ok = "✅" if dv >= 0 else "❌"
            lines.append(
                f"| {d}天 | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
                f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} "
                f"| {r['score']-bf['score']:+.4f} | {ok}{dex if seg == 'oos' else ''} |")
    out = OUT_DIR / f"pulse_debt_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    import uuid
    main()
