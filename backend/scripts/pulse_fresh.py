# -*- coding: utf-8 -*-
"""排序键 fresh（金叉新鲜度）按 P0 流程验证：OAT → AB 双段 → 与 P4 叠加复验。

基座 = bt_124c6d43225a 形态；已采纳 P4（slot_rotation_on=on, slot_stale_days=5）。
判定链：
  1) OAT：FRESH 全区间 Δscore > 0 → 进 AB
  2) AB：OOS 超额 ≥ 基座 OOS 超额(-0.30%) 且全区间 Δscore > 0 → 过线
  3) 叠加：FRESH+P4 vs P4 形态，OOS 超额 ≥ P4 的 +10.97% → 采纳
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_oat import OOS_SPLIT, _pct, base_cfg, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}

# 参照数字（pulse_oat/pulse_ab/pulse_combine 已验证，不重跑）
BASE_FULL = {"score": -0.2993, "total_return": -0.3264, "excess_return": -0.5411,
             "max_drawdown": -0.6057}
BASE_OOS = {"score": -0.1111, "total_return": 0.2891, "excess_return": -0.0030,
            "max_drawdown": -0.2052}
P4_FULL = {"score": -0.1318, "total_return": 0.1134, "excess_return": -0.1012,
           "max_drawdown": -0.4523}
P4_OOS = {"score": -0.1571, "total_return": 0.4018, "excess_return": 0.1097,
          "max_drawdown": -0.1422}


def mk(ov_top: dict, oos: bool, tag: str) -> dict:
    cfg = base_cfg()
    cfg.update(ov_top)
    if oos:
        cfg["start_date"] = OOS_SPLIT
    cfg["name"] = tag
    return cfg


def main():
    rows, verdict = [], []
    # 1) OAT：FRESH 全区间
    print("[OAT] FRESH 全区间 ...", flush=True)
    fresh_full = run_score(mk({"auto_rank_key": "fresh"}, False, "pulse_fresh_full"))
    d = fresh_full["score"] - BASE_FULL["score"]
    rows.append(("OAT-FRESH-全区间", fresh_full, BASE_FULL))
    print(f"  Δscore={d:+.4f}", flush=True)
    if d <= 0:
        print("OAT 无正增量，流程终止（不进 AB）", flush=True)
        _report(rows, verdict, "OAT 未过线")
        return

    # 2) AB：FRESH OOS
    print("[AB] FRESH OOS ...", flush=True)
    fresh_oos = run_score(mk({"auto_rank_key": "fresh"}, True, "pulse_fresh_oos"))
    rows.append(("AB-FRESH-OOS", fresh_oos, BASE_OOS))
    ok = ((fresh_oos.get("excess_return") or -9) >= BASE_OOS["excess_return"])
    print(f"  OOS 超额 {_pct(fresh_oos.get('excess_return'))} vs 基座 {_pct(BASE_OOS['excess_return'])} "
          f"→ {'过线' if ok else '不过'}", flush=True)
    if not ok:
        verdict.append("FRESH 单项 OOS 不过线，不采纳")
        _report(rows, verdict, "FRESH AB 不过线")
        return
    verdict.append("FRESH 单项过线（OOS 超额不低于基座）")

    # 3) 叠加：FRESH+P4 双段，参照 = P4 形态
    print("[叠加] FRESH+P4 全区间 + OOS ...", flush=True)
    fp_full = run_score(mk({**P4, "auto_rank_key": "fresh"}, False, "pulse_freshP4_full"))
    fp_oos = run_score(mk({**P4, "auto_rank_key": "fresh"}, True, "pulse_freshP4_oos"))
    rows.append(("叠加-FRESH+P4-全区间", fp_full, P4_FULL))
    rows.append(("叠加-FRESH+P4-OOS", fp_oos, P4_OOS))
    dex_f = (fp_full.get("excess_return") or 0) - P4_FULL["excess_return"]
    dex_o = (fp_oos.get("excess_return") or 0) - P4_OOS["excess_return"]
    ok2 = (fp_oos.get("excess_return") or -9) >= P4_OOS["excess_return"]
    print(f"  vs P4：全区间 Δ超额={dex_f:+.2%}｜OOS Δ超额={dex_o:+.2%} "
          f"→ {'采纳' if ok2 else '不采纳'}", flush=True)
    verdict.append("FRESH+P4 vs P4：OOS " + ("过线，可采纳" if ok2 else "不过线，保留 P4 原形态"))
    _report(rows, verdict, "完成")


def _report(rows, verdict, status):
    lines = [
        "# 排序键 fresh（金叉新鲜度）P0 流程验证报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
        f"OOS = {OOS_SPLIT} 起｜基座 = bt_124c6d43225a｜已采纳机制 = P4 轮动 stale5",
        f"- 流程状态：{status}",
        "",
        "| 步骤 | 段 | score | 总收益 | 超额 | 参照超额 | Δ超额 | 回撤 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for step, res, ref in rows:
        dex = (res.get("excess_return") or 0) - (ref.get("excess_return") or 0)
        lines.append(
            f"| {step} | {'OOS' if 'OOS' in step else '全区间'} | {res['score']:.4f} "
            f"| {_pct(res.get('total_return'))} | {_pct(res.get('excess_return'))} "
            f"| {_pct(ref.get('excess_return'))} | {dex:+.2%} "
            f"| {_pct(res.get('max_drawdown'))} |")
    lines += ["", "## 判定链", ""] + [f"- {v}" for v in verdict]
    out = OUT_DIR / f"pulse_fresh_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
