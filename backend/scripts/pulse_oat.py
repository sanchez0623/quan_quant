# -*- coding: utf-8 -*-
"""P0 脉冲行情优化 OAT（docs 无：对话方案 P0 五项，单项扰动、防过拟合切窗目标）。

基座 = bt_124c6d43225a 用户微调形态（动态选股 zz500），区间同任务
2022-09-11~2026-09-10。五项实验：
  P1 止盈降档   risk.take_profit_pct 40 -> 15 / 25
  P2 追踪收紧   risk.atr_trail_mult 6.0 -> 3 / 4.5
  P3 市场状态机 params.market_regime_on=on + core_scale_range 0.5 / 0.3（crash 档连带生效）
  P4 排名轮动   params.slot_rotation_on=on + slot_stale_days 3 / 5
  P5 试仓先行   params.base_pct_min 10 -> 5
score = stage1 切窗超额目标（5 窗 mean − 0.5×std − dd_floor 罚）。
判定：各项最优档 Δscore > 0 进 AB（全区间 + OOS 双段对照）。

用法（backend/）：python scripts/pulse_oat.py [--ab]（--ab 只跑 OOS 采纳段对照）
输出：scripts/out/pulse_oat_<ts>.md + out/pulse_oat_rows.jsonl（断点续跑）。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage1_oat import _score  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_oat_rows.jsonl"
BASE_REPORT = Path(__file__).resolve().parents[2] / "data" / "reports" / "bt_124c6d43225a.json"
OOS_SPLIT = "2025-07-03"

# (项名, [档位 override dicts])；override 键 = (落位, key)
EXPERIMENTS = [
    ("P1_止盈降档", [
        {("risk", "take_profit_pct"): 15},
        {("risk", "take_profit_pct"): 25},
    ]),
    ("P2_追踪收紧", [
        {("risk", "atr_trail_mult"): 3},
        {("risk", "atr_trail_mult"): 4.5},
    ]),
    ("P3_市场状态机", [
        {("params", "market_regime_on"): "on", ("params", "core_scale_range"): 0.5},
        {("params", "market_regime_on"): "on", ("params", "core_scale_range"): 0.3},
    ]),
    ("P4_排名轮动", [
        {("params", "slot_rotation_on"): "on", ("params", "slot_stale_days"): 3},
        {("params", "slot_rotation_on"): "on", ("params", "slot_stale_days"): 5},
    ]),
    ("P5_试仓先行", [
        {("params", "base_pct_min"): 5},
    ]),
]


def base_cfg() -> dict:
    cfg = json.loads(BASE_REPORT.read_text(encoding="utf-8"))["config"]
    cfg["name"] = "pulse_oat_base"
    return cfg


def apply_ov(cfg: dict, ov: dict, tag: str) -> dict:
    out = json.loads(json.dumps(cfg))
    for (where, key), v in ov.items():
        if where == "params":
            out["params"][key] = v
        elif where == "risk":
            out["risk_config"][key] = v
        else:
            out[key] = v
    out["name"] = tag
    return out


def run_score(cfg: dict) -> dict:
    rep = runner.run_backtest(cfg)
    s = _score(rep)
    m = rep.get("metrics", {}) or {}
    s["total_return"] = m.get("total_return")
    s["excess_return"] = m.get("excess_return")
    s["max_drawdown"] = m.get("max_drawdown")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab", action="store_true",
                    help="AB 阶段：正增量项跑全区间+OOS 双段对照")
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

    cfg0 = base_cfg()
    rows = []
    if not args.ab:
        combo = "BASE"
        if combo in done:
            base = done[combo]
            print(f"基座（缓存）：score={base['score']:.4f}", flush=True)
        else:
            print("[0/11] 基座回测 ...", flush=True)
            base = run_score(cfg0)
            base["combo"] = combo
            done[combo] = base
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(base, ensure_ascii=False) + "\n")
        bscore = base["score"]
        total = 1 + sum(len(vs) for _, vs in EXPERIMENTS)
        cnt = 1
        for name, variants in EXPERIMENTS:
            for vi, ov in enumerate(variants, 1):
                combo = f"{name}#{vi}"
                cnt += 1
                if combo in done:
                    r = done[combo]
                    print(f"[{cnt}/{total}] {combo}（缓存）score={r['score']:.4f}", flush=True)
                    rows.append({"name": name, "vi": vi, **r})
                    continue
                tag = f"pulse_{name.split('_')[0]}_v{vi}"
                r = run_score(apply_ov(cfg0, ov, tag))
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

    # ---- AB 阶段：正增量项 全区间 + OOS 双段 ----
    base_full = done.get("BASE")
    if not base_full:
        raise RuntimeError("缺基座结果，先跑 OAT 阶段")
    winners = []
    for name, variants in EXPERIMENTS:
        best = None
        for vi, ov in enumerate(variants, 1):
            r = done.get(f"{name}#{vi}")
            if r and (best is None or r["score"] > best[1]["score"]):
                best = (vi, r, ov)
        if best and best[1]["score"] > base_full["score"] + 1e-9:
            winners.append((name, *best))
    print(f"正增量项 {len(winners)} 个：{[w[0] for w in winners]}", flush=True)

    print("[AB] 基座 OOS 段回测 ...", flush=True)
    oos_cfg = json.loads(json.dumps(cfg0))
    oos_cfg["start_date"] = OOS_SPLIT
    oos_cfg["name"] = "pulse_base_oos"
    base_oos = run_score(oos_cfg)
    print(f"  基座 OOS score={base_oos['score']:.4f}", flush=True)

    ab_rows = []
    for name, vi, _r, ov in winners:
        tag = f"pulse_ab_{name.split('_')[0]}_v{vi}"
        print(f"[AB] {name} v{vi} 全区间 + OOS ...", flush=True)
        full = run_score(apply_ov(cfg0, ov, tag + "_full"))
        oosc = apply_ov(oos_cfg, ov, tag + "_oos")
        oos = run_score(oosc)
        ab_rows.append({"name": name, "vi": vi, "full": full, "oos": oos,
                        "base_oos": base_oos})
        print(f"  全区间 Δscore={full['score']-base_full['score']:+.4f} "
              f"Δ超额={(full['excess_return'] or 0)-(base_full['excess_return'] or 0):+.2%} | "
              f"OOS Δscore={oos['score']-base_oos['score']:+.4f}", flush=True)
    _report_ab(base_full, base_oos, ab_rows)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _row_str(r: dict) -> str:
    return (f"score {r['score']:.4f}｜总收益 {_pct(r.get('total_return'))}｜"
            f"超额 {_pct(r.get('excess_return'))}｜回撤 {_pct(r.get('max_drawdown'))}")


def _pct(v) -> str:
    return f"{v:+.2%}" if isinstance(v, (int, float)) else "-"


def _report(done: dict, rows: list, bscore: float) -> None:
    lines = [
        "# P0 脉冲行情优化 OAT 报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 基座 = bt_124c6d43225a（动态 zz500 + 用户微调形态）｜区间 2022-09-11~2026-09-10"
        "｜score = 5 窗超额 mean − 0.5×std − dd 罚",
        f"- 基座 {_row_str(done['BASE'])}",
        "",
        "| 项 | 档 | score | Δscore | 总收益 | 超额 | 回撤 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_name: dict[str, list] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    for name, variants in EXPERIMENTS:
        best_d = None
        for r in by_name.get(name, []):
            d = r["score"] - bscore
            if best_d is None or d > best_d:
                best_d = d
        for r in by_name.get(name, []):
            d = r["score"] - bscore
            lines.append(
                f"| {name} | v{r['vi']} | {r['score']:.4f} | {d:+.4f} "
                f"| {_pct(r.get('total_return'))} | {_pct(r.get('excess_return'))} "
                f"| {_pct(r.get('max_drawdown'))} "
                f"| {'✅ 进AB' if d == best_d and best_d > 0 else '❌'} |")
    lines += [
        "",
        "## 备注",
        "",
        "- P3 开启 market_regime_on 时 crash 档（core_scale_crash=0.4）连带生效，属开关整体效果",
        "- 判定 = 各项最优档 Δscore > 0 进 AB（全区间+OOS 双段，OOS 采纳线另算）",
        "- 单项单验，绝不叠加（FULL 叠加已四次复现崩盘）",
    ]
    out = OUT_DIR / f"pulse_oat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(base_full: dict, base_oos: dict, ab_rows: list) -> None:
    lines = [
        "# P0 脉冲优化 AB 对照报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
        f"OOS = {OOS_SPLIT} 起（全区间 70% 分位）",
        "",
        f"- 基座全区间：{_row_str(base_full)}",
        f"- 基座 OOS：{_row_str(base_oos)}",
        "",
        "| 项 | 段 | score | Δscore | 总收益 | 超额 | Δ超额 | 回撤 | 采纳 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], base_full),
                               ("OOS", r["oos"], base_oos)):
            d = res["score"] - bref["score"]
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = (res.get("excess_return") or -9) >= (bref.get("excess_return") or 9)
            lines.append(
                f"| {r['name']}v{r['vi']} | {seg} | {res['score']:.4f} | {d:+.4f} "
                f"| {_pct(res.get('total_return'))} | {_pct(res.get('excess_return'))} "
                f"| {dex:+.2%} | {_pct(res.get('max_drawdown'))} "
                f"| {'✅' if seg == 'OOS' and ok else ('—' if seg == 'OOS' else '—')} |")
    lines += [
        "",
        "## 采纳判定",
        "",
        "- 采纳标准：OOS 段超额 ≥ 基座 OOS 超额（不劣于基座）且全区间 Δscore > 0",
        "- 双段都过的项才可并入用户形态；并入后再逐项叠加复验（仍禁 FULL）",
    ]
    out = OUT_DIR / f"pulse_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
