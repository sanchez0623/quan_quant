# -*- coding: utf-8 -*-
"""自适应止损语境实验：bt_f70e4260e8b2 形态上 adaptive trend→vol（波动率分位）。

基座 = bt_f70e4260e8b2 形态（score 排序+P4+asym_bias0+fwd_t on，棘轮开，adaptive=trend）。
语境变体 VOL：risk_config.adaptive="vol"（vol_n 120/hi 0.7/lo 0.3/k 1.5/0.7 用现值）。

流程：--stage1 语境双段验证（VOL vs 基座）→ --stage2 P0 五项 OAT → --ab 正增量双段。
判定纪律：AB 采纳线 = OOS 超额不低于语境基座。
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
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_vol_rows.jsonl"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}


def base_trend() -> dict:
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["params"].update(P4)
    cfg["params"]["asym_bias"] = 0.0
    cfg["name"] = "vol_base_trend"
    return cfg


def apply_vol(cfg: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    out["risk_config"]["adaptive"] = "vol"
    out["name"] = tag
    return out


def _apply_ov(cfg: dict, ov: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    for k, v in ov.items():
        if k.startswith("risk."):
            out["risk_config"][k[5:]] = v
        else:
            out["params"][k[2:]] = v
    out["name"] = tag
    return out


def _p0_variants() -> list[tuple[str, dict]]:
    return [
        ("P1_止盈15", {"risk.take_profit_pct": 15}),
        ("P1_止盈25", {"risk.take_profit_pct": 25}),
        ("P2_追踪3", {"risk.atr_trail_mult": 3}),
        ("P2_追踪4.5", {"risk.atr_trail_mult": 4.5}),
        ("P3_状态机0.5", {"p.market_regime_on": "on", "p.core_scale_range": 0.5}),
        ("P3_状态机0.3", {"p.market_regime_on": "on", "p.core_scale_range": 0.3}),
        ("P5_试仓先行", {"p.base_pct_min": 5}),
    ]


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

    cfg0 = base_trend()
    vcfg = apply_vol(cfg0, "vol_ctx")

    if args.stage1:
        run_and_log(cfg0, "BASE_full", "基座(trend自适应) 全区间")
        run_and_log(apply_vol(cfg0, "vol_full"), "VOL_full", "VOL语境 全区间")
        run_and_log({**cfg0, "start_date": OOS_SPLIT, "name": "vol_base_oos"},
                    "BASE_oos", "基座(trend自适应) OOS")
        run_and_log(apply_vol({**cfg0, "start_date": OOS_SPLIT}, "vol_oos"),
                    "VOL_oos", "VOL语境 OOS")
        _report_stage1(done)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # stage2/ab 前置：语境必须已双段过线
    bf, vo = done.get("BASE_full"), done.get("VOL_full")
    bo, vv = done.get("BASE_oos"), done.get("VOL_oos")
    if not (bf and vo and bo and vv):
        raise RuntimeError("缺 stage1 结果，先跑 --stage1")
    ok = (vo["score"] > bf["score"]
          and (vv.get("excess_return") or -9) >= (bo.get("excess_return") or 9))
    print(f"语境判定：VOL vs 基座 → 全区间 Δscore {vo['score']-bf['score']:+.4f}｜"
          f"OOS Δ超额 {(vv.get('excess_return') or 0)-(bo.get('excess_return') or 0):+.2%} "
          f"({'✅ 语境成立' if ok else '❌ 语境不成立'})", flush=True)
    if not ok:
        print("语境不成立，stage2/ab 终止", flush=True)
        return

    bs, boos_ex = vo["score"], vv.get("excess_return") or 0
    if args.stage2:
        total = len(_p0_variants())
        rows = []
        for i, (combo, ov) in enumerate(_p0_variants(), 1):
            r = run_and_log(_apply_ov(vcfg, ov, f"volP0_{i}"), combo,
                            f"[{i}/{total}] {combo}")
            print(f"  {combo} score={r['score']:.4f} "
                  f"(Δ{r['score']-bs:+.4f}, {time.time()-t0:,.0f}s)", flush=True)
            rows.append({"combo": combo, **r})
        _report_stage2(done, rows, bs)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # ---- ab ----
    winners = [(c, ov) for c, ov in _p0_variants()
               if (r := done.get(c)) and r["score"] > bs + 0.01]
    print(f"正增量项 {len(winners)} 个：{[w[0] for w in winners]}", flush=True)
    ab_rows = []
    for combo, ov in winners:
        full = run_and_log(_apply_ov(vcfg, ov, f"volAB_{combo}_full"),
                           f"AB_{combo}_full", f"[AB] {combo} 全区间")
        oos = run_and_log(_apply_ov(apply_vol({**cfg0, "start_date": OOS_SPLIT},
                                              "x"), ov, f"volAB_{combo}_oos"),
                          f"AB_{combo}_oos", f"[AB] {combo} OOS")
        ab_rows.append({"combo": combo, "full": full, "oos": oos})
        print(f"  {combo} 全区间 Δ超额="
              f"{(full.get('excess_return') or 0)-(vo.get('excess_return') or 0):+.2%} | "
              f"OOS Δ超额={(oos.get('excess_return') or 0)-boos_ex:+.2%}", flush=True)
    _report_ab(done, ab_rows)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _rs(r: dict) -> str:
    return (f"score {r['score']:.4f}｜总收益 {_pct(r.get('total_return'))}｜"
            f"超额 {_pct(r.get('excess_return'))}｜回撤 {_pct(r.get('max_drawdown'))}")


def _report_stage1(done: dict) -> None:
    lines = [
        "# 自适应止损语境验证（adaptive trend→vol，波动率分位）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_f70e4260e8b2 形态（adaptive=trend，棘轮开）｜VOL = adaptive=vol"
        "（vol_n120/hi0.7/lo0.3/k1.5/0.7）",
        f"- 基座全区间：{_rs(done['BASE_full'])}",
        f"- 基座 OOS：{_rs(done['BASE_oos'])}",
        "",
        "| 语境 | 段 | score | 总收益 | 超额 | 回撤 | vs基座 |",
        "|---|---|---|---|---|---|---|",
    ]
    for seg in ("full", "oos"):
        r = done.get(f"VOL_{seg}")
        b = done.get(f"BASE_{seg}")
        if not (r and b):
            continue
        lines.append(
            f"| VOL | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
            f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} "
            f"| score {r['score']-b['score']:+.4f} / 超额 "
            f"{(r.get('excess_return') or 0)-(b.get('excess_return') or 0):+.2%} |")
    lines += ["", "## 判定", "",
              "- 全区间 Δscore>0 且 OOS Δ超额≥0 → 语境成立，进 stage2"]
    out = OUT_DIR / f"pulse_vol_stage1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_stage2(done: dict, rows: list, bs: float) -> None:
    lines = [
        "# P0 五项 OAT（VOL 语境：adaptive=vol）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 语境基座：{_rs(done['VOL_full'])}",
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
    out = OUT_DIR / f"pulse_vol_stage2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(done: dict, ab_rows: list) -> None:
    vo = done["VOL_full"]
    lines = [
        "# VOL 语境 AB 对照",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- VOL 语境基座 全区间：{_rs(vo)}",
        f"- VOL 语境基座 OOS：{_rs(done['VOL_oos'])}",
        "",
        "| 组合 | 段 | score | 超额 | vs语境基座超额 | 采纳 |",
        "|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], vo),
                               ("OOS", r["oos"], done["VOL_oos"])):
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = seg == "OOS" and dex >= 0
            lines.append(
                f"| {r['combo']} | {seg} | {res['score']:.4f} "
                f"| {_pct(res.get('excess_return'))} | {dex:+.2%} "
                f"| {'✅' if ok else ('❌' if seg == 'OOS' else '—')} |")
    out = OUT_DIR / f"pulse_vol_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
