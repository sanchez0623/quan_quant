# -*- coding: utf-8 -*-
"""槽位语境实验：max_holdings 6→8/10，base_pct 联动等权（尽量平均持仓）。

联动原则（用户要求"尽量平均持仓"）：
- 满配态等权：base_pct_max = floor(可投资金 98.5% / N)，满配总仓 ≈ 96%/95%，不触单票上限 17%
- 试仓带宽：base_pct_min = min(原试仓档, max×0.8) 向下取整到 0.5
- pool_n 修复：原 6 == max_holdings（轮动补位池偏小），S8→12 / S10→15（> max_holdings）
- max_holdings 两处同步（params + risk_config，引擎风控为最终屏障）

流程：--stage1 槽位语境双段验证（S8/S10 vs 6槽基座）→ --stage2 最优槽位下 P0 OAT → --ab
判定纪律同前：AB 采纳线 = OOS 超额不低于语境基座。
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
from pulse_ratchet_oat import base_ratchet, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_slots_rows.jsonl"

# 槽位变体：两处 max_holdings 同步 + base_pct 联动等权 + pool_n 修复
SLOT_VARIANTS = {
    "S8": {"holdings": 8, "base_max": 12, "base_min": 10, "pool_n": 12},
    "S10": {"holdings": 10, "base_max": 9.5, "base_min": 7.5, "pool_n": 15},
}


def apply_slots(cfg: dict, spec: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    out["params"]["max_holdings"] = spec["holdings"]
    out["risk_config"]["max_holdings"] = spec["holdings"]  # 两处同步
    out["params"]["base_pct_min"] = spec["base_min"]
    out["params"]["base_pct_max"] = spec["base_max"]
    out["params"]["pool_n"] = spec["pool_n"]
    out["name"] = tag
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1", action="store_true")
    ap.add_argument("--stage2", action="store_true")
    ap.add_argument("--ab", action="store_true")
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
    b6f = done.get("BASE6_full") or run_and_log(cfg0, "BASE6_full",
                                                "6槽基座 全区间")
    if not args.stage2 and not args.ab:
        # ---- stage1：S8/S10 双段 ----
        for name, spec in SLOT_VARIANTS.items():
            run_and_log(apply_slots(cfg0, spec, f"slots_{name}_full"),
                        f"{name}_full", f"{name} 全区间")
            oos_cfg = apply_slots({**cfg0, "start_date": OOS_SPLIT},
                                  spec, f"slots_{name}_oos")
            run_and_log(oos_cfg, f"{name}_oos", f"{name} OOS")
        _report_stage1(done)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # ---- 选最优槽位（全区间 score + OOS 超额双段比较 6 槽基座）----
    winner, wspec = _pick_slot(done)
    print(f"槽位语境胜者：{winner}", flush=True)
    base_oos = done.get("BASE6_oos") or run_and_log(
        {**cfg0, "start_date": OOS_SPLIT, "name": "slots_base6_oos"},
        "BASE6_oos", "6槽基座 OOS")
    bscore, boos_ex = b6f["score"], base_oos.get("excess_return") or 0

    if args.stage2:
        # ---- stage2：最优槽位语境下 P0 OAT ----
        wcfg = apply_slots(cfg0, wspec, f"slots_{winner}_base")
        wfull = done.get(f"{winner}_full")
        bs = wfull["score"]
        variants = _p0_variants(wspec)
        total = 1 + len(variants)
        rows = []
        for i, (combo, ov) in enumerate(variants, 1):
            if combo in done:
                r = done[combo]
            else:
                r = run_and_log(_apply_ov(wcfg, ov, f"slotsP0_{i}"), combo,
                                f"[{i}/{total}] {combo}")
            print(f"  {combo} score={r['score']:.4f} (Δ{r['score']-bs:+.4f})",
                  flush=True)
            rows.append({"combo": combo, **r})
        _report_stage2(done, winner, wspec, rows, bs)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # ---- ab：正增量项双段 ----
    winners = []
    for combo, ov in _p0_variants(wspec):
        r = done.get(combo)
        if r and r["score"] > done.get(f"{winner}_full", {}).get("score", 9) + 0.01:
            winners.append((combo, ov))
    print(f"正增量项 {len(winners)} 个：{[w[0] for w in winners]}", flush=True)
    ab_rows = []
    for combo, ov in winners:
        full = run_and_log(_apply_ov(apply_slots(cfg0, wspec, "x"), ov,
                                     f"slotsAB_{combo}_full"),
                           f"AB_{combo}_full", f"[AB] {combo} 全区间")
        oos = run_and_log(_apply_ov(apply_slots({**cfg0, "start_date": OOS_SPLIT},
                                                wspec, "x"), ov,
                                    f"slotsAB_{combo}_oos"),
                          f"AB_{combo}_oos", f"[AB] {combo} OOS")
        ab_rows.append({"combo": combo, "full": full, "oos": oos})
        print(f"  {combo} 全区间 Δ超额="
              f"{(full.get('excess_return') or 0)-(b6f.get('excess_return') or 0):+.2%} | "
              f"OOS Δ超额={(oos.get('excess_return') or 0)-boos_ex:+.2%}", flush=True)
    _report_ab(done, winner, b6f, base_oos, ab_rows)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def run_and_log(cfg: dict, combo: str, desc: str) -> dict:
    if combo in _cached():
        print(f"[缓存] {desc}", flush=True)
        return _cached()[combo]
    print(f"[回测] {desc} ...", flush=True)
    r = run_score(cfg)
    r["combo"] = combo
    with ROWS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _CACHE[combo] = r
    print(f"  → {desc}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}｜"
          f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
    return r


_CACHE: dict[str, dict] = {}


def _cached() -> dict:
    return _CACHE


def _pick_slot(done: dict) -> tuple[str, dict]:
    """双段比较：全区间 score + OOS 超额，任一段劣于 6 槽基座即淘汰。"""
    b6f = done["BASE6_full"]
    best = None
    for name, spec in SLOT_VARIANTS.items():
        f, o = done.get(f"{name}_full"), done.get(f"{name}_oos")
        if not f or not o:
            continue
        if (f["score"] > b6f["score"]
                and (o.get("excess_return") or -9) >= (done["BASE6_oos"].get("excess_return") or 9)):
            if best is None or f["score"] > done[f"{best}_full"]["score"]:
                best = name
    return (best, SLOT_VARIANTS[best]) if best else ("S8", SLOT_VARIANTS["S8"])


def _p0_variants(spec: dict) -> list[tuple[str, dict]]:
    """P0 五项（P4 已在基座）：最优槽位语境下的扰动档。"""
    bmin = spec["base_min"]
    return [
        ("P1_止盈15", {"risk.take_profit_pct": 15}),
        ("P1_止盈25", {"risk.take_profit_pct": 25}),
        ("P2_追踪3", {"risk.atr_trail_mult": 3}),
        ("P2_追踪4.5", {"risk.atr_trail_mult": 4.5}),
        ("P3_状态机0.5", {"p.market_regime_on": "on", "p.core_scale_range": 0.5}),
        ("P3_状态机0.3", {"p.market_regime_on": "on", "p.core_scale_range": 0.3}),
        ("P5_试仓先行", {"p.base_pct_min": round(bmin * 0.6, 1)}),
    ]


def _apply_ov(cfg: dict, ov: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    for k, v in ov.items():
        if k.startswith("risk."):
            out["risk_config"][k[5:]] = v
        else:
            out["params"][k[2:]] = v
    out["name"] = tag
    return out


def _rs(r: dict) -> str:
    return (f"score {r['score']:.4f}｜总收益 {_pct(r.get('total_return'))}｜"
            f"超额 {_pct(r.get('excess_return'))}｜回撤 {_pct(r.get('max_drawdown'))}")


def _report_stage1(done: dict) -> None:
    lines = [
        "# 槽位语境验证（S8/S10 vs 6槽基座，等权联动）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 联动：S8=8槽×12%(pool_n12)｜S10=10槽×9.5%(pool_n15)；max_holdings 两处同步",
        f"- 6槽基座(现状,base 10/20 不等权)：{_rs(done['BASE6_full'])}",
        "",
        "| 槽位 | 段 | score | 总收益 | 超额 | 回撤 | vs基座 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in SLOT_VARIANTS:
        for seg in ("full", "oos"):
            r = done.get(f"{name}_{seg}")
            if not r:
                continue
            b = done.get(f"BASE6_{seg}") or done["BASE6_full"]
            lines.append(
                f"| {name} | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
                f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} "
                f"| score {r['score']-b['score']:+.4f} / 超额 "
                f"{(r.get('excess_return') or 0)-(b.get('excess_return') or 0):+.2%} |")
    out = OUT_DIR / f"pulse_slots_stage1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_stage2(done: dict, winner: str, spec: dict, rows: list,
                   bs: float) -> None:
    lines = [
        f"# P0 五项 OAT（{winner} 槽位语境，等权联动）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 语境基座（{winner}：{spec['holdings']}槽×{spec['base_max']}%）：{_rs(done[f'{winner}_full'])}",
        "- P4 已在基座；判定 = Δscore > 0.01 进 AB",
        "",
        "| 组合 | score | Δscore | 总收益 | 超额 | 回撤 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        d = r["score"] - bs
        lines.append(
            f"| {r['combo']} | {r['score']:.4f} | {d:+.4f} "
            f"| {_pct(r.get('total_return'))} | {_pct(r.get('excess_return'))} "
            f"| {_pct(r.get('max_drawdown'))} | {'✅ 进AB' if d > 0.01 else '❌'} |")
    out = OUT_DIR / f"pulse_slots_stage2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(done: dict, winner: str, b6f: dict, base_oos: dict,
               ab_rows: list) -> None:
    wf, wo = done[f"{winner}_full"], done[f"{winner}_oos"]
    lines = [
        f"# 槽位语境 AB 对照（{winner}）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 6槽基座 全区间：{_rs(b6f)}",
        f"- {winner} 语境基座 全区间：{_rs(wf)}",
        f"- {winner} 语境基座 OOS：{_rs(wo)}",
        "",
        "| 组合 | 段 | score | 超额 | vs语境基座超额 | 采纳 |",
        "|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], wf), ("OOS", r["oos"], wo)):
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = seg == "OOS" and dex >= 0
            lines.append(
                f"| {r['combo']} | {seg} | {res['score']:.4f} "
                f"| {_pct(res.get('excess_return'))} | {dex:+.2%} "
                f"| {'✅' if ok else ('❌' if seg == 'OOS' else '—')} |")
    out = OUT_DIR / f"pulse_slots_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
