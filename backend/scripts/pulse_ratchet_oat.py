# -*- coding: utf-8 -*-
"""棘轮关语境 P0 重测：基座 = bt_f70e4260e8b2 形态 + atr_trail_floor=False（分钟）。

背景：P1/P2 在棘轮开语境被 OOS 拦截（样本内陷阱）；棘轮关放松移动止损后，
止盈/追踪类参数的边际行为可能改变，值得在"棘轮关"前提下重测。
P4（轮动 stale5）已固化在基座，不重复扰动。

判定纪律同 P0：OAT Δscore>0 → AB 双段（OOS 超额不低于棘轮关基座）→ 过线项叠加复验。
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
from pulse_oat import base_cfg  # noqa: E402
from stage1_oat import _score  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_ratchet_rows.jsonl"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}

# P0 五项（P4 已在基座；其余四项 7 档）
EXPERIMENTS = [
    ("P1_止盈降档", [
        {"take_profit_pct": 15},
        {"take_profit_pct": 25},
    ]),
    ("P2_追踪收紧", [
        {"atr_trail_mult": 3},
        {"atr_trail_mult": 4.5},
    ]),
    ("P3_市场状态机", [
        {"market_regime_on": "on", "core_scale_range": 0.5},
        {"market_regime_on": "on", "core_scale_range": 0.3},
    ]),
    ("P5_试仓先行", [
        {"base_pct_min": 5},
    ]),
]


def base_ratchet() -> dict:
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["params"].update(P4)
    cfg["params"]["asym_bias"] = 0.0
    cfg["risk_config"]["atr_trail_floor"] = False  # 棘轮关：止损线可随最高价回落
    cfg["name"] = "pulse_ratchet_base"
    return cfg


def run_score(cfg: dict) -> dict:
    rep = runner.run_backtest(cfg)
    s = _score(rep)
    m = rep.get("metrics", {}) or {}
    s["total_return"] = m.get("total_return")
    s["excess_return"] = m.get("excess_return")
    s["max_drawdown"] = m.get("max_drawdown")
    return s


def apply_ov(cfg: dict, ov: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    for k, v in ov.items():
        if k in ("take_profit_pct", "atr_trail_mult"):
            out["risk_config"][k] = v
        else:
            out["params"][k] = v
    out["name"] = tag
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab", action="store_true", help="AB：正增量项双段对照")
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
        print(f"续跑：已有 {len(done)} 条结果", flush=True)

    cfg0 = base_ratchet()
    print(f"基座棘轮检查：atr_trail_floor = "
          f"{cfg0['risk_config'].get('atr_trail_floor')}", flush=True)

    if not args.ab:
        total = 1 + sum(len(vs) for _, vs in EXPERIMENTS)
        if "BASE" in done:
            base = done["BASE"]
            print(f"基座（缓存）：score={base['score']:.4f}", flush=True)
        else:
            print(f"[0/{total}] 基座回测（棘轮关）...", flush=True)
            base = run_score(cfg0)
            base["combo"] = "BASE"
            done["BASE"] = base
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(base, ensure_ascii=False) + "\n")
        bscore = base["score"]
        cnt, rows = 1, []
        for name, variants in EXPERIMENTS:
            for vi, ov in enumerate(variants, 1):
                combo = f"{name}#{vi}"
                cnt += 1
                if combo in done:
                    r = done[combo]
                else:
                    r = run_score(apply_ov(cfg0, ov, f"ratchet_{name.split('_')[0]}_v{vi}"))
                    r["combo"] = combo
                    done[combo] = r
                    with ROWS_JSONL.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                print(f"[{cnt}/{total}] {combo} score={r['score']:.4f} "
                      f"(Δ{r['score']-bscore:+.4f}, {time.time()-t0:,.0f}s)", flush=True)
                rows.append({"name": name, "vi": vi, **r})
        _report(done, rows, bscore)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # ---- AB：正增量项双段 ----
    base_full = done.get("BASE")
    if not base_full:
        raise RuntimeError("缺基座结果，先跑 OAT")
    winners = []
    for name, variants in EXPERIMENTS:
        best = None
        for vi, ov in enumerate(variants, 1):
            r = done.get(f"{name}#{vi}")
            if r and (best is None or r["score"] > best[1]["score"]):
                best = (vi, r, ov)
        if best and best[1]["score"] > base_full["score"] + 0.01:
            winners.append((name, *best))
    print(f"正增量项 {len(winners)} 个：{[w[0] for w in winners]}", flush=True)

    print("[AB] 棘轮关基座 OOS ...", flush=True)
    cfg_b = json.loads(json.dumps(cfg0))
    cfg_b["start_date"] = OOS_SPLIT
    cfg_b["name"] = "pulse_ratchet_base_oos"
    base_oos = run_score(cfg_b)
    print(f"  基座 OOS score={base_oos['score']:.4f}", flush=True)

    ab_rows = []
    for name, vi, _r, ov in winners:
        print(f"[AB] {name} v{vi} 全区间 + OOS ...", flush=True)
        full = run_score(apply_ov(cfg0, ov, f"ratchet_ab_{name.split('_')[0]}_v{vi}_full"))
        oos = run_score(apply_ov(
            {**cfg0, "start_date": OOS_SPLIT},
            ov, f"ratchet_ab_{name.split('_')[0]}_v{vi}_oos"))
        ab_rows.append({"name": name, "vi": vi, "full": full, "oos": oos})
        print(f"  全区间 Δscore={full['score']-base_full['score']:+.4f} "
              f"Δ超额={(full.get('excess_return') or 0)-(base_full.get('excess_return') or 0):+.2%} | "
              f"OOS Δscore={oos['score']-base_oos['score']:+.4f} "
              f"Δ超额={(oos.get('excess_return') or 0)-(base_oos.get('excess_return') or 0):+.2%}",
              flush=True)
    _report_ab(base_full, base_oos, ab_rows)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _rs(r: dict) -> str:
    return (f"score {r['score']:.4f}｜总收益 {_pct(r.get('total_return'))}｜"
            f"超额 {_pct(r.get('excess_return'))}｜回撤 {_pct(r.get('max_drawdown'))}")


def _report(done: dict, rows: list, bscore: float) -> None:
    lines = [
        "# 棘轮关语境 P0 重测 OAT 报告（分钟）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 基座 = bt_f70e4260e8b2 形态（P4+asym_bias0+fwd_t on）+ **atr_trail_floor=False（棘轮关）**"
        "｜动态 zz500｜minute5",
        f"- 基座 {_rs(done['BASE'])}",
        "- P4 已固化在基座不重复扰动；背景对照：棘轮开基座 score -0.2014",
        "",
        "| 项 | 档 | score | Δscore | 总收益 | 超额 | 回撤 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_name: dict[str, list] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    for name, variants in EXPERIMENTS:
        best_d = max((r["score"] - bscore for r in by_name.get(name, [])),
                     default=None)
        for r in by_name.get(name, []):
            d = r["score"] - bscore
            lines.append(
                f"| {name} | v{r['vi']} | {r['score']:.4f} | {d:+.4f} "
                f"| {_pct(r.get('total_return'))} | {_pct(r.get('excess_return'))} "
                f"| {_pct(r.get('max_drawdown'))} "
                f"| {'✅ 进AB' if d == best_d and best_d > 0 else '❌'} |")
    lines += ["", "## 备注", "",
              "- 判定 = 各项最优档 Δscore > 0 进 AB；AB 采纳线 = OOS 超额不低于棘轮关基座"]
    out = OUT_DIR / f"pulse_ratchet_oat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(base_full: dict, base_oos: dict, ab_rows: list) -> None:
    lines = [
        "# 棘轮关语境 AB 对照报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "",
        f"- 基座(棘轮关)全区间：{_rs(base_full)}",
        f"- 基座(棘轮关)OOS：{_rs(base_oos)}",
        "- 背景对照：棘轮开基座 全区间 score -0.2014/超额 -33.90%",
        "",
        "| 项 | 档 | 段 | score | Δscore | 超额 | Δ超额 | 回撤 | 采纳 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], base_full),
                               ("OOS", r["oos"], base_oos)):
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = (res.get("excess_return") or -9) >= (bref.get("excess_return") or 9)
            lines.append(
                f"| {r['name']} | v{r['vi']} | {seg} | {res['score']:.4f} "
                f"| {res['score']-bref['score']:+.4f} | {_pct(res.get('excess_return'))} "
                f"| {dex:+.2%} | {_pct(res.get('max_drawdown'))} "
                f"| {'✅' if seg == 'OOS' and ok else ('❌' if seg == 'OOS' else '—')} |")
    lines += ["", "## 判定", "",
              "- OOS 超额 ≥ 棘轮关基座 OOS 超额 且全区间 Δscore > 0 → 过线",
              "- 多过线项逐项叠加复验后再定最终形态（禁 FULL）"]
    out = OUT_DIR / f"pulse_ratchet_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
