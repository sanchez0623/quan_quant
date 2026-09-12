# -*- coding: utf-8 -*-
"""fwd_t（正向T开关）按 P0 流程验证——分钟语境（period=minute5，做T层只在分钟生效）。

注意：分钟与日线口径不可跨比，判定链全部在分钟语境内部自建：
  0) BASE_M5：基座（动态 zz500，t_mode 默认 grid=反向T在跑、fwd_t off）全区间——兼冒烟
  1) OAT：FWD_T（fwd_t=on，预算默认 25%）全区间 Δscore > 0 → 进 AB
  2) AB：FWD_T OOS 超额 ≥ BASE_M5 OOS 超额 → 过线
  3) 叠加：BASE_M5+P4 与 FWD_T+P4 双段对照，OOS 超额 ≥ BASE_M5+P4 → 采纳
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_oat import OOS_SPLIT, _pct, base_cfg, run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
P4 = {"slot_rotation_on": "on", "slot_stale_days": 5}
FWD = {"fwd_t": "on"}


def mk5(ov_params: dict, oos: bool, tag: str) -> dict:
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["params"].update(ov_params)
    if oos:
        cfg["start_date"] = OOS_SPLIT
    cfg["name"] = tag
    return cfg


def main():
    t0 = time.time()
    rows, verdict = [], []

    # 0) BASE_M5 全区间（兼冒烟：动态+分钟组合首次运行）
    print("[0] BASE_M5 全区间（动态+分钟首次运行，兼冒烟）...", flush=True)
    b5 = run_score(mk5({}, False, "pulse_fwdt_baseM5_full"))
    rows.append(("BASE_M5-全区间", b5, None))
    print(f"  score={b5['score']:.4f} 超额 {_pct(b5.get('excess_return'))} "
          f"({time.time()-t0:,.0f}s)", flush=True)

    # 1) OAT：FWD_T 全区间
    print("[1] FWD_T 全区间 ...", flush=True)
    f5 = run_score(mk5(FWD, False, "pulse_fwdt_on_full"))
    d = f5["score"] - b5["score"]
    rows.append(("FWD_T-全区间", f5, b5))
    print(f"  Δscore={d:+.4f}", flush=True)
    if d <= 0:
        verdict.append("FWD_T OAT 无正增量，不采纳")
        _report(rows, verdict, "OAT 未过线")
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return

    # 2) AB：FWD_T OOS vs BASE_M5 OOS
    print("[2] BASE_M5 OOS + FWD_T OOS ...", flush=True)
    b5o = run_score(mk5({}, True, "pulse_fwdt_baseM5_oos"))
    f5o = run_score(mk5(FWD, True, "pulse_fwdt_on_oos"))
    rows.append(("BASE_M5-OOS", b5o, None))
    rows.append(("FWD_T-OOS", f5o, b5o))
    ok = (f5o.get("excess_return") or -9) >= (b5o.get("excess_return") or 9)
    dex = (f5o.get("excess_return") or 0) - (b5o.get("excess_return") or 0)
    print(f"  FWD_T OOS 超额 {_pct(f5o.get('excess_return'))} vs BASE_M5 "
          f"{_pct(b5o.get('excess_return'))}（Δ{dex:+.2%}）→ {'过线' if ok else '不过'}",
          flush=True)
    if not ok:
        verdict.append(f"FWD_T OOS 不过线（Δ超额 {dex:+.2%}），不采纳")
        _report(rows, verdict, "FWD_T AB 不过线")
        print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)
        return
    verdict.append("FWD_T 单项过线（OOS 超额不低于 BASE_M5）")

    # 3) 叠加：BASE_M5+P4 vs FWD_T+P4 双段
    print("[3] 叠加对照：BASE_M5+P4 与 FWD_T+P4（全区间 + OOS）...", flush=True)
    bp4_f = run_score(mk5(P4, False, "pulse_fwdt_baseP4_full"))
    bp4_o = run_score(mk5(P4, True, "pulse_fwdt_baseP4_oos"))
    fp4_f = run_score(mk5({**P4, **FWD}, False, "pulse_fwdt_fwdP4_full"))
    fp4_o = run_score(mk5({**P4, **FWD}, True, "pulse_fwdt_fwdP4_oos"))
    rows += [("BASE_M5+P4-全区间", bp4_f, None), ("BASE_M5+P4-OOS", bp4_o, None),
             ("FWD_T+P4-全区间", fp4_f, bp4_f), ("FWD_T+P4-OOS", fp4_o, bp4_o)]
    ok2 = (fp4_o.get("excess_return") or -9) >= (bp4_o.get("excess_return") or 9)
    print(f"  vs BASE_M5+P4：OOS 超额 {_pct(fp4_o.get('excess_return'))} vs "
          f"{_pct(bp4_o.get('excess_return'))} → {'采纳' if ok2 else '不采纳'}", flush=True)
    verdict.append("FWD_T+P4 vs BASE_M5+P4：OOS " +
                   ("过线，可采纳" if ok2 else "不过线，P4 形态不加正向T"))
    _report(rows, verdict, "完成")
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(rows, verdict, status):
    lines = [
        "# fwd_t（正向T开关）P0 流程验证报告（分钟语境 period=minute5）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
        f"OOS = {OOS_SPLIT} 起｜基座 = bt_124c6d43225a 动态 zz500 形态（切分钟）",
        f"- 流程状态：{status}",
        "- 口径说明：分钟与日线不可跨比；判定链全部在分钟语境内部自建。"
        "t_mode 默认 grid（反向T/网格T）在两个对照组都在跑，单变量 = fwd_t。",
        "",
        "| 步骤 | 段 | score | 总收益 | 超额 | 参照超额 | Δ超额 | 回撤 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for step, res, ref in rows:
        dex = ((res.get("excess_return") or 0) -
               ((ref or {}).get("excess_return") or 0))
        lines.append(
            f"| {step} | {'OOS' if step.endswith('OOS') else '全区间'} "
            f"| {res['score']:.4f} | {_pct(res.get('total_return'))} "
            f"| {_pct(res.get('excess_return'))} "
            f"| {_pct((ref or {}).get('excess_return'))} | {dex:+.2%} "
            f"| {_pct(res.get('max_drawdown'))} |")
    lines += ["", "## 判定链", ""] + [f"- {v}" for v in verdict]
    lines += ["", "- 背景：做T层在静态 150 子集 zz500 域两次验证无效（stageT OAT / v5 重验）；"
              "本次为动态选股新语境的首测"]
    out = OUT_DIR / f"pulse_fwdt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
