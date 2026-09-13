# -*- coding: utf-8 -*-
"""做T层过线项渐进叠加复验（动态+分钟）：按 OOS 强度降序逐项并入，每步全区间+OOS。

带 jsonl 断点续跑（原生层崩溃后看门狗重启不丢步）。
判定：每步 OOS 超额（底线 = 基座 -40.61%），观察累积趋势，最终取 OOS 最优步。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_t_oat import base_m5, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_t_combine_rows.jsonl"
BASE_FULL = {"score": -0.3063, "total_return": -0.3744, "excess_return": -0.5890,
             "max_drawdown": -0.5059}
BASE_OOS = {"score": -0.5300, "total_return": -0.1140, "excess_return": -0.4061,
            "max_drawdown": -0.3573}

STEPS = [
    ("asym_bias0", {"asym_bias": 0.0}),
    ("+t_mode_time", {"t_mode": "time"}),
    ("+grid_atr_mult0.4", {"grid_atr_mult": 0.4}),
    ("+vol_grid_lo0.95", {"vol_grid_lo": 0.95}),
    ("+vol_q_lo0.4", {"vol_q_lo": 0.4}),
    ("+atr_period7", {"atr_period": 7}),
    ("+vol_window60", {"vol_window": 60}),
]


def main():
    t0 = time.time()
    cfg0 = base_m5()
    done: dict[str, dict] = {}
    if ROWS_JSONL.exists():
        for line in ROWS_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done[r["combo"]] = r
            except Exception:
                continue
        print(f"续跑：已有 {len(done)} 条结果", flush=True)

    rows = []
    for name, ov in STEPS:
        combo_f, combo_o = f"{name}#full", f"{name}#oos"
        cfg = json.loads(json.dumps(cfg0))
        cfg["params"].update(ov)
        cfg["name"] = f"pulse_t_cmb_{name}_full"
        if combo_f in done:
            full = done[combo_f]
        else:
            print(f"[{name}] 全区间 ...", flush=True)
            full = run_score(cfg)
            full["combo"] = combo_f
            done[combo_f] = full
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")
        c2 = json.loads(json.dumps(cfg))
        c2["start_date"] = OOS_SPLIT
        c2["name"] = f"pulse_t_cmb_{name}_oos"
        if combo_o in done:
            oos = done[combo_o]
        else:
            print(f"[{name}] OOS ...", flush=True)
            oos = run_score(c2)
            oos["combo"] = combo_o
            done[combo_o] = oos
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(oos, ensure_ascii=False) + "\n")
        rows.append({"name": name, "full": full, "oos": oos})
        print(f"  [{name}] 全区间 超额 {_pct(full.get('excess_return'))}｜"
              f"OOS 超额 {_pct(oos.get('excess_return'))}（vs 基座 -40.61%）", flush=True)

    lines = [
        "# 做T层过线项渐进叠加复验报告（动态+分钟）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座：score -0.3063｜超额 -58.90%（全区间）；OOS 超额 -40.61%",
        "",
        "| 步 | 段 | score | 总收益 | 超额 | Δ超额(vs基座) | 回撤 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        for seg, res, bref in (("全区间", r["full"], BASE_FULL),
                               ("OOS", r["oos"], BASE_OOS)):
            dex = (res.get("excess_return") or 0) - bref["excess_return"]
            lines.append(
                f"| {r['name']} | {seg} | {res['score']:.4f} "
                f"| {_pct(res.get('total_return'))} | {_pct(res.get('excess_return'))} "
                f"| {dex:+.2%} | {_pct(res.get('max_drawdown'))} |")
    lines += [
        "",
        "## 判定",
        "",
        "- 底线：每步 OOS 超额 ≥ 基座 -40.61%；最终形态取 OOS 最优步（叠加不再增益即止步）",
        "- 禁 FULL：任一步 OOS 显著劣化即止步于上一步",
    ]
    out = OUT_DIR / f"pulse_t_combine_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
