# -*- coding: utf-8 -*-
"""P0 过线项逐项叠加复验：P4(轮动) → +P3(状态机) → +P5(试仓)，每步全区间+OOS。"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_oat import OOS_SPLIT, _pct, base_cfg, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"

P4 = {("params", "slot_rotation_on"): "on", ("params", "slot_stale_days"): 5}
P3 = {("params", "market_regime_on"): "on", ("params", "core_scale_range"): 0.3}
P5 = {("params", "base_pct_min"): 5}

STEPS = [
    ("P4", P4),
    ("P4+P3", {**P4, **P3}),
    ("P4+P3+P5", {**P4, **P3, **P5}),
]

# 基座参照（pulse_ab 报告数字，直接引用不再重跑）
BASE_FULL = {"score": -0.2993, "total_return": -0.3264, "excess_return": -0.5411,
             "max_drawdown": -0.6057}
BASE_OOS = {"score": -0.1111, "total_return": 0.2891, "excess_return": -0.0030,
            "max_drawdown": -0.2052}


def main():
    t0 = time.time()
    cfg0 = base_cfg()
    rows = []
    for name, ov in STEPS:
        full_cfg = json.loads(json.dumps(cfg0))
        for (w, k), v in ov.items():
            (full_cfg["params"] if w == "params" else full_cfg["risk_config"])[k] = v
        full_cfg["name"] = f"pulse_cmb_{name}_full"
        print(f"[{name}] 全区间 ...", flush=True)
        full = run_score(full_cfg)
        oos_cfg = json.loads(json.dumps(full_cfg))
        oos_cfg["start_date"] = OOS_SPLIT
        oos_cfg["name"] = f"pulse_cmb_{name}_oos"
        print(f"[{name}] OOS ...", flush=True)
        oos = run_score(oos_cfg)
        rows.append({"name": name, "full": full, "oos": oos})
        print(f"  全区间 Δ超额={(full.get('excess_return') or 0)-(BASE_FULL['excess_return']):+.2%} "
              f"| OOS Δ超额={(oos.get('excess_return') or 0)-(BASE_OOS['excess_return']):+.2%} "
              f"| OOS score {oos['score']:.4f} vs 基座 {BASE_OOS['score']:.4f}", flush=True)

    lines = [
        "# P0 过线项叠加复验报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
        f"OOS = {OOS_SPLIT} 起｜路径：P4 → +P3 → +P5",
        "",
        f"- 基座全区间：总收益 {_pct(BASE_FULL['total_return'])}｜超额 {_pct(BASE_FULL['excess_return'])}",
        f"- 基座 OOS：总收益 {_pct(BASE_OOS['total_return'])}｜超额 {_pct(BASE_OOS['excess_return'])}",
        "",
        "| 组合 | 段 | score | 总收益 | 超额 | Δ超额 | 回撤 | 采纳 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        for seg, res, bref in (("全区间", r["full"], BASE_FULL),
                               ("OOS", r["oos"], BASE_OOS)):
            dex = (res.get("excess_return") or 0) - bref["excess_return"]
            ok = (res.get("excess_return") or -9) >= bref["excess_return"]
            lines.append(
                f"| {r['name']} | {seg} | {res['score']:.4f} "
                f"| {_pct(res.get('total_return'))} | {_pct(res.get('excess_return'))} "
                f"| {dex:+.2%} | {_pct(res.get('max_drawdown'))} "
                f"| {'✅' if seg == 'OOS' and ok else ('❌' if seg == 'OOS' else '—')} |")
    lines += ["", "## 判定", "",
              "- OOS 超额 ≥ 基座 OOS 超额（-0.30%）的组合可并入用户形态",
              "- 任一步 OOS 崩坏则止步于上一步（逐项叠加，禁 FULL）"]
    out = OUT_DIR / f"pulse_combine_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
