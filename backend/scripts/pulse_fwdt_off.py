# -*- coding: utf-8 -*-
"""fwd_t 反向对照：基座已是 on（用户配置），测关闭（fwd_t=off）是否更优——分钟语境。

判定：off 的 OOS 超额 vs 基座(on) 的 OOS 超额——
  off 更差 → 正向T有正贡献，保持 on；
  off 更好 → 正向T是拖累，建议关闭（用户拍板）。
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
OFF = {"fwd_t": "off"}
# 基座(on)全区间参照（pulse_fwdt 首轮 BASE_M5，分钟语境）
BASE_FULL = {"score": -0.2014, "total_return": -0.1244, "excess_return": -0.3390,
             "max_drawdown": None}


def main():
    t0 = time.time()
    cfg0 = base_cfg()
    rows = []

    print("[1] FWD_OFF 全区间 ...", flush=True)
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["params"].update(OFF)
    cfg["name"] = "pulse_fwdt_off_full"
    off_full = run_score(cfg)
    rows.append(("FWD_OFF-全区间", off_full, BASE_FULL))
    print(f"  Δscore={off_full['score']-BASE_FULL['score']:+.4f}", flush=True)

    print("[2] 基座(on) OOS + FWD_OFF OOS ...", flush=True)
    cfg_b = base_cfg()
    cfg_b["period"] = "minute5"
    cfg_b["start_date"] = OOS_SPLIT
    cfg_b["name"] = "pulse_fwdt_on_oos"
    base_oos = run_score(cfg_b)
    cfg_o = json.loads(json.dumps(cfg))
    cfg_o["start_date"] = OOS_SPLIT
    cfg_o["name"] = "pulse_fwdt_off_oos"
    off_oos = run_score(cfg_o)
    rows.append(("基座on-OOS", base_oos, None))
    rows.append(("FWD_OFF-OOS", off_oos, base_oos))
    dex = (off_oos.get("excess_return") or 0) - (base_oos.get("excess_return") or 0)
    keep = (off_oos.get("excess_return") or -9) < (base_oos.get("excess_return") or 9)
    print(f"  on OOS 超额 {_pct(base_oos.get('excess_return'))}｜"
          f"off OOS 超额 {_pct(off_oos.get('excess_return'))}（Δ{dex:+.2%}）→ "
          f"{'正向T有正贡献，保持 on' if keep else '正向T是拖累，建议关'}", flush=True)

    lines = [
        "# fwd_t 反向对照报告（分钟语境）：关闭正向T vs 基座(on)",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
        f"OOS = {OOS_SPLIT} 起｜基座 = bt_124c6d43225a（fwd_t 原本就是 on，723 笔正向T/4年）",
        "- 口径：分钟语境内部对照（t_mode=grid、max_t_times=6 两组一致，单变量 = fwd_t）",
        "",
        f"- 基座(on) 全区间：score {BASE_FULL['score']:.4f}｜"
        f"总收益 {_pct(BASE_FULL['total_return'])}｜超额 {_pct(BASE_FULL['excess_return'])}",
        "",
        "| 组 | 段 | score | 总收益 | 超额 | Δ超额 | 回撤 |",
        "|---|---|---|---|---|---|---|",
    ]
    for step, res, ref in rows:
        dex2 = ((res.get("excess_return") or 0) -
                ((ref or {}).get("excess_return") or 0))
        lines.append(
            f"| {step} | {'OOS' if step.endswith('OOS') else '全区间'} "
            f"| {res['score']:.4f} | {_pct(res.get('total_return'))} "
            f"| {_pct(res.get('excess_return'))} "
            f"| {_pct((ref or {}).get('excess_return'))} | {dex2:+.2%} "
            f"| {_pct(res.get('max_drawdown'))} |")
    lines += [
        "",
        "## 判定",
        "",
        (f"- off 的 OOS 超额 {_pct(off_oos.get('excess_return'))} vs on "
         f"{_pct(base_oos.get('excess_return'))}（Δ{dex:+.2%}）→ "
         + ("**正向T有正贡献，保持 on 不动**" if keep
            else "**正向T为拖累，建议关闭（待拍板）**")),
        "- 全区间 Δscore = "
        f"{off_full['score']-BASE_FULL['score']:+.4f}",
    ]
    out = OUT_DIR / f"pulse_fwdt_off_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
