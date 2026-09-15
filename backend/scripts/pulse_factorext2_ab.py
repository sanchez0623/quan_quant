# -*- coding: utf-8 -*-
"""AB 复验：影线承接升级 z1.0 / z0.75（用户拍板两档都验，独立重跑双段确认位级）。

复验 = 独立重跑（非缓存）与 OAT 缓存位级对齐校验（防缓存/传参事故）。
采纳档（z1.0 vs z0.75）由用户在复验结果上拍板，本脚本不改名不打标。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_dynsel_oat import ROWS_JSONL as DYN_ROWS, mk_dyn  # noqa: E402
from pulse_factorext2_oat import ROWS_JSONL as OAT_ROWS  # noqa: E402
from pulse_fwdt import OOS_SPLIT, _pct  # noqa: E402
from pulse_gateoff_oat import base_gate_on  # noqa: E402
from stage1_oat import _score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"

AB_ITEMS = [
    ("z1.0", {"shadow_confirm": "on", "shadow_z_confirm": 1.0}, "z1p0"),
    ("z0.75", {"shadow_confirm": "on", "shadow_z_confirm": 0.75}, "z0p75"),
]


def load_oat() -> dict:
    oat = {}
    for path in (DYN_ROWS, OAT_ROWS):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                oat[r["combo"]] = r
            except Exception:
                continue
    return oat


def main():
    t0 = time.time()
    OUT_DIR.mkdir(exist_ok=True)
    oat = load_oat()

    cfg0 = base_gate_on()
    rows = []  # (name, tag, r, oat_combo)

    def run_ab(name: str, overrides: dict | None, oos: bool, oat_combo: str):
        tag = "oos" if oos else "full"
        cfg = mk_dyn(cfg0, oos, auto=True, top=50)
        if overrides:
            cfg["params"].update(overrides)
            cfg["name"] = f"ab_fe2_{name.replace('.', 'p')}_{tag}"
        else:
            cfg["name"] = f"ab_fe2_base_{tag}"
        from app.engine import runner
        rep = runner.run_backtest(cfg)
        s = _score(rep)
        m = rep.get("metrics", {}) or {}
        r = {"score": s["score"], "total_return": m.get("total_return"),
             "excess_return": m.get("excess_return"), "max_drawdown": m.get("max_drawdown")}
        rows.append((name, tag, r, oat_combo))
        print(f"[AB] {name} {tag}: score {r['score']:.4f}｜超额 {_pct(r.get('excess_return'))}", flush=True)

    run_ab("基线(因子关)", None, False, "TOP50_全区间")
    run_ab("基线(因子关)", None, True, "TOP50_OOS段")
    for name, overrides, oat_pfx in AB_ITEMS:
        run_ab(f"承接升级{name}", overrides, False, f"{oat_pfx}_全区间")
        run_ab(f"承接升级{name}", overrides, True, f"{oat_pfx}_OOS段")

    _report(rows, oat)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _align(r: dict, oat_r: dict | None) -> str:
    if not oat_r:
        return "OAT缺"
    same = abs(r["score"] - oat_r["score"]) < 1e-6 and \
        abs((r.get("excess_return") or 0) - (oat_r.get("excess_return") or 0)) < 1e-6
    return "✓位级一致" if same else f"✗偏差 OAT={oat_r['score']:.4f}"


def _report(rows, oat: dict) -> None:
    lines = [
        "# AB 复验：影线承接升级 z1.0 / z0.75（基座=采纳形态10项TOP50包，独立重跑）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜OOS = {OOS_SPLIT} 起",
        "- 用户拍板两档都验；采纳档待复验结果拍板（z1.0 赢 full 全面性 / z0.75 赢 OOS 强度）",
        "",
        "| 项 | 段 | score | 超额 | 回撤 | OAT 对齐 |",
        "|---|---|---|---|---|---|",
    ]
    for name, tag, r, oat_combo in rows:
        lines.append(
            f"| {name} | {tag} | {r['score']:.4f} | {_pct(r.get('excess_return'))} "
            f"| {_pct(r.get('max_drawdown'))} | {_align(r, oat.get(oat_combo))} |")
    lines += ["", "## 判定", "",
              "- 两档位级一致 → 复验通过，采纳档由用户拍板（本轮脚本不改名不打标）。"]
    out = OUT_DIR / f"pulse_factorext2_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
