# -*- coding: utf-8 -*-
"""P0 五项最优档合并回测：在当前最优形态（bt_f70e4260e8b2 = 用户形态+P4+asym_bias0，分钟）上验证。

组合：
  COMBO5 = 基座 + P1(止盈15) + P2(追踪4.5) + P3(状态机0.3) + P5(试仓5)   [P4 已在基座]
  COMBO3 = 基座 + P3 + P5                                              [仅过线项]
参照：基座 bt_f70e4260e8b2 全区间 +4.39%/超额 -17.07%；OOS +30.58%/+1.37%。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import OOS_SPLIT, _pct, base_cfg, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}
P1 = {"take_profit_pct": 15}
P2 = {"atr_trail_mult": 4.5}
P3 = {"market_regime_on": "on", "core_scale_range": 0.3}
P5 = {"base_pct_min": 5}
BASE_FULL = {"score": None, "total_return": 0.0439, "excess_return": -0.1707,
             "max_drawdown": None}
BASE_OOS = {"score": None, "total_return": 0.3058, "excess_return": 0.0137,
            "max_drawdown": None}

COMBOS = [
    ("COMBO5五项全叠", {**P1, **P2, **P3, **P5}),
    ("COMBO3仅过线项", {**P3, **P5}),
]


def main():
    t0 = time.time()
    cfg0 = base_cfg()
    cfg0["period"] = "minute5"
    cfg0["params"].update(P4)
    cfg0["params"]["asym_bias"] = 0.0  # 当前最优形态 = bt_f70e4260e8b2 形态
    rows = []
    for name, ov in COMBOS:
        cfg = json.loads(json.dumps(cfg0))
        for k, v in ov.items():
            if k in ("take_profit_pct", "atr_trail_mult"):
                cfg["risk_config"][k] = v
            else:
                cfg["params"][k] = v
        cfg["name"] = f"pulse_combo_{name}_full"
        print(f"[{name}] 全区间 ...", flush=True)
        full = run_score(cfg)
        c2 = json.loads(json.dumps(cfg))
        c2["start_date"] = OOS_SPLIT
        c2["name"] = f"pulse_combo_{name}_oos"
        print(f"[{name}] OOS ...", flush=True)
        oos = run_score(c2)
        rows.append({"name": name, "full": full, "oos": oos})
        print(f"  [{name}] 全区间 收益 {_pct(full.get('total_return'))} 超额 "
              f"{_pct(full.get('excess_return'))}｜OOS 收益 {_pct(oos.get('total_return'))} "
              f"超额 {_pct(oos.get('excess_return'))}", flush=True)

    lines = [
        "# P0 五项最优档合并回测报告（分钟语境，基座=bt_f70e4260e8b2 形态）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 基座（on形态）全区间：收益 +4.39%｜超额 -17.07%；OOS：收益 +30.58%｜超额 +1.37%",
        "- 注意：P1/P2 的最优档在 AB 阶段被 OOS 否决（样本内陷阱），合并仅作效果验证",
        "",
        "| 组合 | 段 | score | 总收益 | 超额 | Δ超额(vs基座) | 回撤 |",
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
        "- 若 COMBO5 OOS 显著劣于基座 → P1/P2 的样本内陷阱在合并中兑现",
        "- 若 COMBO3 OOS ≥ 基座 → 过线项可并入当前最优形态（再叠加复验定稿）",
    ]
    out = OUT_DIR / f"pulse_combo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
