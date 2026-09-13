# -*- coding: utf-8 -*-
"""动态语境做T层 OAT（5 分钟线）：基座 = bt_124c6d43225a + P4 轮动（已采纳形态）+ minute5。

背景：静态 150 子集语境下做T层两次验证无效（stageT OAT / v5 重验）；动态语境 fwd_t
反向对照显示正向T有显著正贡献（OOS +20.7pt），做T层参数需在动态语境下重查。
判定纪律与 P0 一致：单项 OAT Δscore>0 → AB 全区间+OOS 双段 → OOS 超额不低于基座才过线；
多过线项逐项叠加复验（禁 FULL）。

用法（backend/）：
  python scripts/pulse_t_oat.py           # OAT 全量（46 次回测 ~31 分钟）
  python scripts/pulse_t_oat.py --ab      # AB：正增量项双段对照
输出：out/pulse_t_oat_<ts>.md / pulse_t_ab_<ts>.md + pulse_t_oat_rows.jsonl（续跑）
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
ROWS_JSONL = OUT_DIR / "pulse_t_oat_rows.jsonl"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}

# 做T层 19 项（档位沿用 stageT 网格；基座值：t_mode=grid / max_t_times=6 /
# fwd_t=on / trend_clock=intraday / 其余默认，非基座档位才有意义）
GRIDS_T = [
    ("t_mode", ["discipline", "time", "off"]),
    ("max_t_times", [2, 8, 10]),
    ("trend_clock", ["daily"]),
    ("atr_period", [7, 21]),
    ("grid_atr_mult", [0.4, 0.7, 1.3, 1.6]),
    ("grid_floor_pct", [0.2, 0.6, 0.8]),
    ("asym_bias", [0.0, 0.6]),
    ("t_ratio_base", [10, 40]),
    ("t_decay", [0.5, 0.9]),
    ("asym_sell_cap", [1.0, 4.0, 6.0]),
    ("vol_window", [60, 250]),
    ("vol_q_hi", [0.6, 0.8]),
    ("vol_q_lo", [0.2, 0.4]),
    ("vol_grid_hi", [1.1, 1.6]),
    ("vol_grid_lo", [0.6, 0.95]),
    ("t_vol_hi", [1.1, 1.7]),
    ("t_vol_lo", [0.4, 0.9]),
    ("t_debt_max_days", [1, 5, 8]),
    ("t_max_chase_pct", [1.0, 6.0, 10.0]),
]


def base_m5() -> dict:
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["params"].update(P4)
    cfg["name"] = "pulse_t_base"
    return cfg


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

    cfg0 = base_m5()
    if not args.ab:
        total = 1 + sum(len(v) for _, v in GRIDS_T)
        if "BASE" in done:
            base = done["BASE"]
            print(f"基座（缓存）：score={base['score']:.4f}", flush=True)
        else:
            print(f"[0/{total}] 基座回测（动态+分钟+P4）...", flush=True)
            base = run_score(cfg0)
            base["combo"] = "BASE"
            done["BASE"] = base
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(base, ensure_ascii=False) + "\n")
        bscore = base["score"]
        cnt, rows = 1, []
        for key, values in GRIDS_T:
            for vi, v in enumerate(values, 1):
                combo = f"{key}={v}"
                cnt += 1
                if combo in done:
                    r = done[combo]
                else:
                    c = json.loads(json.dumps(cfg0))
                    c["params"][key] = v
                    c["name"] = f"pulse_t_{key}_{v}"
                    r = run_score(c)
                    r["combo"] = combo
                    done[combo] = r
                    with ROWS_JSONL.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                print(f"[{cnt}/{total}] {combo} score={r['score']:.4f} "
                      f"(Δ{r['score']-bscore:+.4f}, {time.time()-t0:,.0f}s)", flush=True)
                rows.append({"key": key, "val": v, **r})
        _report(done, rows, bscore)
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # ---- AB：正增量项双段 ----
    base_full = done.get("BASE")
    if not base_full:
        raise RuntimeError("缺基座结果，先跑 OAT")
    winners = []
    for key, values in GRIDS_T:
        best = None
        for v in values:
            r = done.get(f"{key}={v}")
            if r and (best is None or r["score"] > best[1]["score"]):
                best = (v, r)
        if best and best[1]["score"] > base_full["score"] + 0.01:  # 噪声过滤：Δ<0.01 不进 AB
            winners.append((key, best[0]))
    print(f"正增量项 {len(winners)} 个：{[f'{k}={v}' for k, v in winners]}", flush=True)

    print("[AB] 基座 OOS ...", flush=True)
    cfg_b = json.loads(json.dumps(cfg0))
    cfg_b["start_date"] = OOS_SPLIT
    cfg_b["name"] = "pulse_t_base_oos"
    base_oos = run_score(cfg_b)
    print(f"  基座 OOS score={base_oos['score']:.4f}", flush=True)

    ab_rows = []
    for key, v in winners:
        print(f"[AB] {key}={v} 全区间 + OOS ...", flush=True)
        cf = json.loads(json.dumps(cfg0))
        cf["params"][key] = v
        cf["name"] = f"pulse_t_ab_{key}_full"
        full = run_score(cf)
        co = json.loads(json.dumps(cf))
        co["start_date"] = OOS_SPLIT
        co["name"] = f"pulse_t_ab_{key}_oos"
        oos = run_score(co)
        ab_rows.append({"key": key, "val": v, "full": full, "oos": oos})
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
        "# 动态语境做T层 OAT 报告（5 分钟线）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 基座 = bt_124c6d43225a + P4 轮动 stale5｜动态 zz500｜period=minute5"
        "｜t_mode=grid/fwd_t=on/max_t_times=6（用户原值）",
        f"- 基座 {_rs(done['BASE'])}",
        "",
        "| 参数 | 档 | score | Δscore | 总收益 | 超额 | 回撤 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_key: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    for key, values in GRIDS_T:
        best_d = max((r["score"] - bscore for r in by_key.get(key, [])),
                     default=None)
        for r in by_key.get(key, []):
            d = r["score"] - bscore
            lines.append(
                f"| {key} | {r['val']} | {r['score']:.4f} | {d:+.4f} "
                f"| {_pct(r.get('total_return'))} | {_pct(r.get('excess_return'))} "
                f"| {_pct(r.get('max_drawdown'))} "
                f"| {'✅ 进AB' if d == best_d and best_d > 0 else '❌'} |")
    lines += [
        "",
        "## 备注",
        "",
        "- t_mode=off 档 = 做T层整体关闭（含正向T），测做T层总贡献",
        "- 判定 = 各参数最优档 Δscore > 0 进 AB（双段对照，OOS 超额不低于基座才过线）",
        "- 单项单验禁叠加；多过线项另行逐项叠加复验",
    ]
    out = OUT_DIR / f"pulse_t_oat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


def _report_ab(base_full: dict, base_oos: dict, ab_rows: list) -> None:
    lines = [
        "# 动态语境做T层 AB 对照报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "",
        f"- 基座全区间：{_rs(base_full)}",
        f"- 基座 OOS：{_rs(base_oos)}",
        "",
        "| 参数 | 档 | 段 | score | Δscore | 超额 | Δ超额 | 回撤 | 采纳 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ab_rows:
        for seg, res, bref in (("全区间", r["full"], base_full),
                               ("OOS", r["oos"], base_oos)):
            dex = (res.get("excess_return") or 0) - (bref.get("excess_return") or 0)
            ok = (res.get("excess_return") or -9) >= (bref.get("excess_return") or 9)
            lines.append(
                f"| {r['key']} | {r['val']} | {seg} | {res['score']:.4f} "
                f"| {res['score']-bref['score']:+.4f} | {_pct(res.get('excess_return'))} "
                f"| {dex:+.2%} | {_pct(res.get('max_drawdown'))} "
                f"| {'✅' if seg == 'OOS' and ok else ('❌' if seg == 'OOS' else '—')} |")
    lines += ["", "## 判定", "",
              "- OOS 超额 ≥ 基座 OOS 超额 且全区间 Δscore > 0 → 过线",
              "- 多过线项需逐项叠加复验后再定最终形态（禁 FULL）"]
    out = OUT_DIR / f"pulse_t_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
