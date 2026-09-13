# -*- coding: utf-8 -*-
"""方案 E 实验：regime_b_on=True + trade_tier_on=True（B 档只在趋势市激活，粘滞）。

基座 = bt_76889c798212（trade_tier_on=False）。
单开 regime_b_on 是死操作（is_trade 恒 False，B 档分支永不进入）——
本轮测方案 E 完整体：T 仓 B 档（k1=3.0/k2=5.0/底线10%/退出固定止盈）只在
「趋势市」激活（粘滞：trend 出现一次锁定到平仓），震荡/下跌市退回默认档。
runner 在 regime_b_on=on 时自激活指数 regime 计算（000905，T-1 对齐）。
回答上一轮遗留问题：B 档的拖累（TIERON 双段全劣）是否来自非趋势市。
基线复用 pulse_debt_rows.jsonl（D1 双段，同基座）；无条件 B 档对照复用 pulse_tier_rows.jsonl。

流程：--stage1 语境双段验证 → --stage2 P0 五项 OAT → --ab 正增量双段。
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
from pulse_debt_oat import VOL_ROWS  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_regimeb_rows.jsonl"
DEBT_ROWS = OUT_DIR / "pulse_debt_rows.jsonl"
TIER_ROWS = OUT_DIR / "pulse_tier_rows.jsonl"


def apply_e(cfg: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    out["risk_config"]["trade_tier_on"] = True
    out["risk_config"]["regime_b_on"] = True
    out["name"] = tag
    return out


def _apply_ov(cfg: dict, ov: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    for k, v in ov.items():
        if k.startswith("risk."):
            out["risk_config"][k[5:]] = v
        elif k.startswith("top."):
            out[k[4:]] = v
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
    if TIER_ROWS.exists():
        for line in TIER_ROWS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("combo") in ("TIER_full", "TIER_oos"):
                    base[f"TIER_{r['combo']}"] = r
            except Exception:
                continue
    if "D1_full" not in base or "D1_oos" not in base:
        raise RuntimeError("pulse_debt_rows.jsonl 缺基线（D1_full/D1_oos）")
    bf, bo = base["D1_full"], base["D1_oos"]
    boos_ex = bo.get("excess_return") or 0
    print(f"基线(方案E前)：全区间 score {bf['score']:.4f}/"
          f"超额 {_pct(bf.get('excess_return'))}｜OOS 超额 {_pct(boos_ex)}", flush=True)

    cfg0 = base_gate_on()
    ecfg = apply_e(cfg0, "regimeb_ctx")

    if args.stage1:
        run_and_log(apply_e(cfg0, "E_full"), "E_full", "方案E 全区间")
        run_and_log(apply_e({**cfg0, "start_date": OOS_SPLIT}, "e_oos"),
                    "E_oos", "方案E OOS")
        _report_stage1(done, base, bf, boos_ex)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    ef_, eo_ = done.get("E_full"), done.get("E_oos")
    if not (ef_ and eo_):
        raise RuntimeError("缺 stage1 结果，先跑 --stage1")
    ok = (ef_["score"] > bf["score"]
          and (eo_.get("excess_return") or -9) >= boos_ex)
    print(f"语境判定：方案E vs 基座 → 全区间 Δscore {ef_['score']-bf['score']:+.4f}｜"
          f"OOS Δ超额 {(eo_.get('excess_return') or 0)-boos_ex:+.2%} "
          f"({'✅ 语境成立' if ok else '❌ 语境不成立'})", flush=True)
    if not ok:
        print("语境不成立，stage2/ab 终止", flush=True)
        return

    bs, eoos_ex = ef_["score"], eo_.get("excess_return") or 0
    if args.stage2:
        total = len(_p0_variants())
        rows = []
        for i, (combo, ov) in enumerate(_p0_variants(), 1):
            r = run_and_log(_apply_ov(ecfg, ov, f"regbP0_{i}"), combo,
                            f"[{i}/{total}] {combo}")
            print(f"  {combo} score={r['score']:.4f} "
                  f"(Δ{r['score']-bs:+.4f}, {time.time()-t0:,.0f}s)", flush=True)
            rows.append({"combo": combo, **r})
        _report_stage2(done, rows, bs)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    winners = [(c, ov) for c, ov in _p0_variants()
               if (r := done.get(c)) and r["score"] > bs + 0.01]
    print(f"正增量项 {len(winners)} 个：{[w[0] for w in winners]}", flush=True)
    ab_rows = []
    for combo, ov in winners:
        full = run_and_log(_apply_ov(ecfg, ov, f"regbAB_{combo}_full"),
                           f"AB_{combo}_full", f"[AB] {combo} 全区间")
        oos = run_and_log(_apply_ov(apply_e(
            {**cfg0, "start_date": OOS_SPLIT}, "x"), ov,
            f"regbAB_{combo}_oos"), f"AB_{combo}_oos", f"[AB] {combo} OOS")
        ab_rows.append({"combo": combo, "full": full, "oos": oos})
        print(f"  {combo} 全区间 Δ超额="
              f"{(full.get('excess_return') or 0)-(ef_.get('excess_return') or 0):+.2%} | "
              f"OOS Δ超额={(oos.get('excess_return') or 0)-eoos_ex:+.2%}", flush=True)
    _report_ab(done, ab_rows)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _rs(r: dict) -> str:
    return (f"score {r['score']:.4f}｜总收益 {_pct(r.get('total_return'))}｜"
            f"超额 {_pct(r.get('excess_return'))}｜回撤 {_pct(r.get('max_drawdown'))}")


def _report_stage1(done: dict, base: dict, bf: dict, boos_ex: float) -> None:
    lines = [
        "# 方案 E 语境验证（regime_b_on=True：B 档只在趋势市激活，粘滞）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座 = bt_76889c798212｜方案E = trade_tier_on=True + regime_b_on=True"
        "（T仓B档仅趋势市，震荡/下跌退默认档；runner 自激活 000905 regime，T-1 对齐）",
        f"- 基线全区间：{_rs(bf)}",
        f"- 基线 OOS 超额：{_pct(boos_ex)}",
        "- 三方对照：无条件B档（TIERON，上一轮双段全劣 -42.7%/-0.75%）",
        "",
        "| 语境 | 段 | score | 总收益 | 超额 | 回撤 | vs基线 |",
        "|---|---|---|---|---|---|---|",
    ]
    for seg, b in (("full", bf), ("oos", None)):
        r = done.get(f"E_{seg}")
        if not r:
            continue
        ds = r["score"] - bf["score"]
        dex = ((r.get("excess_return") or 0) - boos_ex) if seg == "oos" else None
        tail = (f"score {ds:+.4f} / 超额 {dex:+.2%}") if seg == "oos" else f"score {ds:+.4f}"
        lines.append(
            f"| 方案E | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
            f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} | {tail} |")
    for seg in ("full", "oos"):
        r = base.get(f"TIER_TIER_{seg}")
        if not r:
            continue
        b = bf if seg == "full" else None
        lines.append(
            f"| 无条件B档(对照) | {seg} | {r['score']:.4f} | {_pct(r.get('total_return'))} "
            f"| {_pct(r.get('excess_return'))} | {_pct(r.get('max_drawdown'))} "
            f"| score {r['score']-bf['score']:+.4f} |")
    lines += ["", "## 判定", "",
              "- 全区间 Δscore>0 且 OOS Δ超额≥0 → 语境成立，进 stage2；否则终止"]
    out = OUT_DIR / f"pulse_regimeb_stage1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_stage2(done: dict, rows: list, bs: float) -> None:
    lines = [
        "# P0 五项 OAT（方案E语境：regime_b_on=True）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 语境基座：{_rs(done['E_full'])}",
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
    out = OUT_DIR / f"pulse_regimeb_stage2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(done: dict, ab_rows: list) -> None:
    ef_ = done["E_full"]
    lines = [
        "# 方案E 语境 AB 对照",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        f"- 方案E 语境基座 全区间：{_rs(ef_)}",
        f"- 方案E 语境基座 OOS：{_rs(done['E_oos'])}",
        "",
        "| 组合 | 段 | score | 超额 | vs语境基座超额 | 采纳 |",
        "|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], ef_),
                               ("OOS", r["oos"], done["E_oos"])):
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = seg == "OOS" and dex >= 0
            lines.append(
                f"| {r['combo']} | {seg} | {res['score']:.4f} "
                f"| {_pct(res.get('excess_return'))} | {dex:+.2%} "
                f"| {'✅' if ok else ('❌' if seg == 'OOS' else '—')} |")
    out = OUT_DIR / f"pulse_regimeb_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
