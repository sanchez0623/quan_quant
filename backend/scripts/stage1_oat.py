# -*- coding: utf-8 -*-
"""阶段 1 OAT 敏感性普查（V3 寻优方案 docs/OPTIMIZE_SLOT_V3_PLAN.md §5.2）。

方法：其它参数固定 BT-D1 基座，单参数走非基座档位网格，每参数 2~4 次回测；
score 为切窗超额目标（5 窗 × 年化超额 mean − 0.5×std − dd_floor 击穿罚），
与寻优阶段目标同构，但由脚本后处理实现、不改 runner。

产出：
- 每参数 I(p)（score 极差）、最优档、孤立尖峰判定（(best−second)/(best−worst) > 0.5）
- 按方案 §5.3 规则 v1 生成"砍半切分预览"（尖峰否决 + 重要性中位补足）
- scripts/out/stage1_oat_rows.jsonl（断点续跑：重跑自动跳过已完成组合）

用法（backend/ 下）：
  python scripts/stage1_oat.py                # 全量普查（~150 次回测，约 30 分钟）
  python scripts/stage1_oat.py --limit 3      # 只跑前 3 个参数（冒烟）
条件参数第二批（market_regime_on=on 的 11 子参数、momentum_fsm_on=on 的
exit_fade_days、slot_rotation_on=on 的 stale/high_window、fixed/trailing 模式的
止损数值）视第一批开关扫描结果决定，不在本脚本。
"""
import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage0_anchors import END_DEFAULT, START_DEFAULT, _cfg, _zz500_universe  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
# 勘误 v4（2026-09-08）：回测起点 2021-07-15（起点前 124 交易日预热自然完成），
# 快照 2021-06-14；与 v3（起点 2021-01-04、预热断供）结果隔离
ROWS_JSONL = OUT_DIR / "stage1_oat_rows_v4.jsonl"

# 切窗目标参数（与方案 §6 一致：三件套在普查即生效）
N_WINDOWS = 5
LAMBDA = 0.5
DD_FLOOR = -0.35
DD_PENALTY_K = 10.0

# ---- 普查清单：(落位, key, 非基座档位) ----
# 落位 params = 策略参数；risk = risk_config；top = 回测顶层
GRIDS = [
    ("params", "pool_n", [2, 10, 14, 18]),
    ("params", "max_holdings", [2, 4, 8, 10]),
    ("params", "atr_stop_k", [-6, -5, -4, -2]),
    ("params", "mom_short", [5, 10, 20, 30]),
    ("params", "mom_mid", [30, 50, 70, 80]),
    ("params", "mom_long", [90, 105, 150, 200]),
    ("params", "w_short", [0.1, 0.2, 0.4, 0.5]),
    ("params", "w_mid", [0.1, 0.2, 0.4, 0.5]),
    ("params", "w_accel", [0.0, 0.1, 0.5, 0.7]),
    ("params", "crash_sigma", [1.0, 1.5, 2.5, 3.0]),
    ("params", "crash_vol_n", [20, 40, 90, 120]),
    ("params", "crash_abs_cap", [10, 20, 45, 60]),
    ("params", "macd_fast", [5, 8, 16, 20]),
    ("params", "macd_slow", [10, 18, 40, 55]),
    ("params", "macd_signal", [3, 6, 12, 15]),
    ("params", "ma_fast", [5, 10, 30, 45]),
    ("params", "slope_n", [2, 3, 8, 10]),
    ("params", "base_pct_min", [5, 15, 25, 35]),
    ("params", "base_pct_max", [20, 30, 60, 80]),
    ("params", "max_adds", [0, 1, 3, 4]),
    ("params", "add_scale", [0.2, 0.35, 0.65, 0.8]),
    ("params", "add_cooldown", [1, 3, 10, 15]),
    ("params", "add_breakout_n", [5, 10, 35, 55]),
    ("params", "exit_need", [1, 3]),
    ("params", "exit_cooldown", [0, 2, 10, 20]),
    ("params", "decay_window", [2, 3, 10, 20]),
    ("params", "decay_pct", [0.05, 0.1, 0.3, 0.5]),
    ("params", "partial_exit_pct", [10, 25, 65, 80]),
    ("params", "exit_confirm_days", [2, 5, 8, 10]),
    ("params", "out_top_days", [1, 2, 3, 5]),
    ("top", "pool_gate_enter_th", [0.05, 0.1, 0.25, 0.35]),
    ("risk", "atr_multiplier", [1.5, 2.0, 3.0, 3.5]),
    ("risk", "atr_trail_mult", [3, 4.5, 8, 10]),
    ("risk", "take_profit_pct", [15, 25, 60, 100]),
    ("risk", "cash_reserve_pct", [0, 5, 10, 20]),
    ("risk", "max_drawdown_breaker", [15, 20, 40, 50]),
    ("params", "momentum_fsm_on", ["on"]),
    ("params", "slot_rotation_on", ["on"]),
    ("top", "pool_gate", [True]),
    ("top", "market_regime_on", ["on"]),
    ("risk", "stop_loss_mode", ["fixed", "atr", "trailing"]),
    ("risk", "adaptive", ["trend", "vol"]),
]


def _score(report: dict) -> dict:
    """切窗超额目标：mean(每窗超额年化) − λ×std − dd_floor 击穿罚。"""
    eq = report.get("equity_curve") or []
    bench = (report.get("benchmark") or {}).get("curve") or []
    if len(eq) < 40 or not bench:
        return {"score": -9e9, "mean_excess": None, "std_excess": None,
                "min_dd": None, "windows": 0}
    bench_map = {str(p.get("date"))[:10]: float(p.get("equity") or p.get("close") or 0)
                 for p in bench}
    pts = []
    for p in eq:
        d = str(p.get("date"))[:10]
        v = p.get("adjusted_equity") or p.get("equity")
        b = bench_map.get(d)
        if v and b and b > 0:
            pts.append((d, float(v), float(b)))
    if len(pts) < 40:
        return {"score": -9e9, "mean_excess": None, "std_excess": None,
                "min_dd": None, "windows": 0}
    n = len(pts)
    edges = [int(i * n / N_WINDOWS) for i in range(N_WINDOWS)] + [n]
    excess, dds = [], []
    for w in range(N_WINDOWS):
        seg = pts[edges[w]:edges[w + 1]]
        if len(seg) < 10:
            continue
        s0, s1 = seg[0][1], seg[-1][1]
        b0, b1 = seg[0][2], seg[-1][2]
        days = len(seg) - 1
        ann_s = (s1 / s0) ** (252.0 / days) - 1 if s0 > 0 and s1 > 0 else -1.0
        ann_b = (b1 / b0) ** (252.0 / days) - 1 if b0 > 0 and b1 > 0 else 0.0
        excess.append(ann_s - ann_b)
        peak = s0
        dd = 0.0
        for _, v, _b in seg:
            peak = max(peak, v)
            if peak > 0:
                dd = min(dd, v / peak - 1)
        dds.append(dd)
    if len(excess) < 2:
        return {"score": -9e9, "mean_excess": None, "std_excess": None,
                "min_dd": None, "windows": len(excess)}
    mean_e = statistics.mean(excess)
    std_e = statistics.stdev(excess)
    worst_dd = min(dds)
    penalty = 0.0
    if worst_dd < DD_FLOOR:
        penalty = (abs(worst_dd) - abs(DD_FLOOR)) * DD_PENALTY_K
    return {"score": mean_e - LAMBDA * std_e - penalty,
            "mean_excess": mean_e, "std_excess": std_e,
            "min_dd": worst_dd, "windows": len(excess)}


def _run_cfg(cfg: dict) -> dict:
    rep = runner.run_backtest(cfg)
    s = _score(rep)
    m = rep.get("metrics", {}) or {}
    s["total_return"] = m.get("total_return")
    s["annual_return"] = m.get("annual_return")
    s["max_drawdown"] = m.get("max_drawdown")
    return s


def _apply(cfg: dict, where: str, key: str, value) -> dict:
    out = json.loads(json.dumps(cfg))  # 深拷贝
    if where == "params":
        out["params"][key] = value
    elif where == "risk":
        out["risk_config"][key] = value
    else:
        out[key] = value
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=END_DEFAULT)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--seed", type=int, default=20260908)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个参数（冒烟）")
    ap.add_argument("--report-only", action="store_true",
                    help="不跑回测，仅从 jsonl 重新生成报告")
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

    uni_all = _zz500_universe(as_of=args.start)
    base_cfg = _cfg("stage1_oat_base", uni_all, start=args.start,
                    end=args.end, capital=args.capital)

    grids = GRIDS[:args.limit] if args.limit else GRIDS
    total = sum(len(v) for _, _, v in grids) + 1

    if args.report_only:
        if "BASE" not in done:
            raise RuntimeError("jsonl 无基座结果，无法出报告")
        _report(done, done["BASE"], args)
        return

    # 基座
    if "BASE" in done:
        base = done["BASE"]
        print(f"基座（缓存）：score={base['score']:.4f}", flush=True)
    else:
        print(f"[0/{total}] 基座回测 ...", flush=True)
        base = _run_cfg(base_cfg)
        base["combo"] = "BASE"
        base["where"] = ""
        base["key"] = "BASE"
        base["value"] = None
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(base, ensure_ascii=False) + "\n")
        print(f"  基座 score={base['score']:.4f} "
              f"(超额均值 {base['mean_excess'] and round(base['mean_excess'], 4)}, "
              f"最差窗回撤 {base['min_dd'] and round(base['min_dd'], 4)})", flush=True)

    cnt, skipped = 0, 0
    for where, key, values in grids:
        for v in values:
            combo = f"{where}.{key}={v}"
            cnt += 1
            if combo in done:
                skipped += 1
                continue
            r = _run_cfg(_apply(base_cfg, where, key, v))
            r.update({"combo": combo, "where": where, "key": key, "value": v})
            done[combo] = r
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[{cnt}/{total}] {combo} -> score={r['score']:.4f} "
                  f"({time.time() - t0:,.0f}s)", flush=True)

    _report(done, base, args)
    print(f"总耗时 {time.time() - t0:,.0f}s（本次实际回测 {cnt - skipped} 次）", flush=True)


def _report(done: dict, base: dict, args) -> None:
    base_score = base["score"]
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in done.values():
        if r["key"] == "BASE":
            continue
        by_key.setdefault((r["where"], r["key"]), []).append(r)

    rows = []
    for (where, key), rs in by_key.items():
        scores = [r["score"] for r in rs] + [base_score]
        best = max(rs, key=lambda r: r["score"])
        worst = min(scores)
        amp = max(scores) - worst
        if amp <= 1e-9:
            isol = 0.0
        else:
            others = sorted(scores, reverse=True)
            isol = (others[0] - others[1]) / (max(scores) - worst)
        rows.append({
            "where": where, "key": key, "n": len(rs),
            "amp": amp, "best_val": best["value"], "best_score": best["score"],
            "base_score": base_score,
            "best_vs_base": best["score"] - base_score,
            "isol": isol, "spike": isol > 0.5,
        })
    rows.sort(key=lambda r: r["amp"], reverse=True)

    half = (len(rows) + 1) // 2
    spikes = [r for r in rows if r["spike"]]
    survivors = [r for r in rows if not r["spike"]][:half]
    cut = [r for r in rows if r["spike"]] + [r for r in rows if not r["spike"]][half:]

    def tag(r):
        return f"`{r['where']}.{r['key']}`"

    lines = [
        "# 阶段 1 OAT 敏感性普查报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 区间：{args.start} ~ {args.end}｜基座 = BT-D1（静态500 + L2）"
        f"｜切窗 {N_WINDOWS} 窗｜λ={LAMBDA}｜dd_floor={DD_FLOOR}",
        f"- 基座 score = {base_score:.4f}（超额均值 "
        f"{base['mean_excess'] and round(base['mean_excess'], 4)}，"
        f"最差窗回撤 {base['min_dd'] and round(base['min_dd'], 4)}）",
        f"- 参数总数 {len(rows)}｜有效组合 {sum(r['n'] for r in rows)} 次",
        "",
        "## 敏感性排名（I(p) = score 极差，降序）",
        "",
        "| # | 参数 | 档位数 | I(p)极差 | 最优档 | 最优score | 相对基座 | 孤立度 | 尖峰 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {tag(r)} | {r['n']} | {r['amp']:.4f} | {r['best_val']} "
            f"| {r['best_score']:.4f} | {r['best_vs_base']:+.4f} "
            f"| {r['isol']:.2f} | {'⚠️' if r['spike'] else ''} |")

    lines += [
        "",
        "## 砍半切分预览（规则 v1：尖峰否决 → 重要性降序补足 ⌈N/2⌉）",
        "",
        f"### 保留（{len(survivors)} 项，进阶段 3 联合寻优）",
        "",
        ", ".join(f"{tag(r)}（I={r['amp']:.3f}）" for r in survivors) or "-",
        "",
        f"### 砍掉（{len(cut)} 项，冻结默认值 / 保守档）",
        "",
        ", ".join(f"{tag(r)}（I={r['amp']:.3f}{'，尖峰' if r['spike'] else ''}）"
                  for r in cut) or "-",
        "",
        "## 备注",
        "",
        "- 孤立度 = (best−second)/(best−worst)：>0.5 判孤立尖峰（历史教训：样本内尖峰=过拟合源），"
        "无论重要性高低强制进砍单",
        "- 第一批为无条件参数；条件参数（market_regime_on=on 的 11 子参数、"
        "momentum_fsm_on/slot_rotation_on=on 的子参数、fixed/trailing 模式止损数值）"
        "视本次开关扫描结果决定是否跑第二批",
        "- 本排名是阶段 2 砍半的输入，最终名单以阶段 2 消融验收（砍半 vs 全参数 OOS 对照）为准",
    ]
    out = OUT_DIR / f"stage1_oat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
