# -*- coding: utf-8 -*-
"""做T层 OAT 敏感性普查（V3 方案 §5.7 L-分钟做T层，用户 2026-09-08 拍板启动）。

背景：日线层全流程（阶段 0-3）已完成，HALF_GATE 为最佳形态；本脚本在分钟线
（做T层唯一生效周期）上回答"做T参数在 zz500 域有没有正解"——校准首测信号
为负（150 只/5.2 年全默认做T：-64.57%，做T盈亏仅 +3.5 万），与 RESEARCH
创科池口径（做T +317.8 万/20 个月）严重矛盾，需参数空间扫描裁决。

设计：
- 基座 = HALF_GATE 分钟形态（2021-06-14 快照随机 150 只子集，period=minute5，
  max_t_times=4 显式）；区间 = 最近四年 2022-09-01~2026-09-07（起点前 ~400
  交易日预热充足）
- OAT：20 项做T层参数 × 非基座档位（47 次）+ 基座 1 次，单次 ~53s
- score = 5 窗超额 mean − 0.5×std − dd_floor 罚（分钟 equity_curve 按日粒度，
  与日线同构）
- 条件参数第二批（reentry_discount 仅 discipline、fwd_t_budget_pct 仅 fwd_t=on、
  t_mode=off/time 下的组交互）视第一批结果决定

用法（backend/ 下）：
  python scripts/stageT_oat.py --limit 3   # 冒烟
  python scripts/stageT_oat.py             # 全量（~48 次回测，约 42 分钟）
输出：scripts/out/stageT_oat_<时间戳>.md + stageT_oat_rows.jsonl（断点续跑）。
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage0_anchors import _cfg, _fmt, _zz500_universe  # noqa: E402
from stage1_oat import _score  # noqa: E402
from stage3_optimize import _apply, _load_half_gate_overrides  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "stageT_oat_rows.jsonl"

START_DEFAULT = "2022-09-11"   # v5 重验区间（与阶段 0-4 对齐；起点前预热充足）
END_DEFAULT = "2026-09-10"
SUBSET = 150
SEED_DEFAULT = 20260908

# 做T层 20 项： (落位, key, 非基座档位)；基座值见各参数 default（t_mode=grid）
GRIDS = [
    ("params", "t_mode", ["discipline", "time", "off"]),
    ("params", "max_t_times", [2, 6, 8, 10]),
    ("params", "trend_clock", ["daily"]),
    ("params", "fwd_t", ["on"]),
    ("params", "atr_period", [7, 21]),
    ("params", "grid_atr_mult", [0.4, 0.7, 1.3, 1.6]),
    ("params", "grid_floor_pct", [0.2, 0.6, 0.8]),
    ("params", "asym_bias", [0.0, 0.6]),
    ("params", "t_ratio_base", [10, 40]),
    ("params", "t_decay", [0.5, 0.9]),
    ("params", "asym_sell_cap", [1.0, 4.0, 6.0]),
    ("params", "vol_window", [60, 250]),
    ("params", "vol_q_hi", [0.6, 0.8]),
    ("params", "vol_q_lo", [0.2, 0.4]),
    ("params", "vol_grid_hi", [1.1, 1.6]),
    ("params", "vol_grid_lo", [0.6, 0.95]),
    ("params", "t_vol_hi", [1.1, 1.7]),
    ("params", "t_vol_lo", [0.4, 0.9]),
    ("params", "t_debt_max_days", [1, 5, 8]),
    ("params", "t_max_chase_pct", [1.0, 6.0, 10.0]),
]


def _base_cfg_m5(uni150: list[str], args) -> dict:
    cfg = _cfg("stageT_base", uni150, start=args.start, end=args.end,
               capital=args.capital)
    cfg["period"] = "minute5"
    half_gate = _load_half_gate_overrides()
    half_gate[("params", "max_t_times")] = 4
    cfg = _apply(cfg, half_gate)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=END_DEFAULT)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--seed", type=int, default=SEED_DEFAULT)
    ap.add_argument("--subset", type=int, default=SUBSET)
    ap.add_argument("--limit", type=int, default=0)
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

    uni = _zz500_universe(as_of=args.start)
    rng = random.Random(args.seed)
    uni_n = sorted(rng.sample(uni, min(args.subset, len(uni))))
    base_cfg = _base_cfg_m5(uni_n, args)

    grids = GRIDS[:args.limit] if args.limit else GRIDS
    total = sum(len(v) for _, _, v in grids) + 1

    if "BASE" in done:
        base = done["BASE"]
        print(f"基座（缓存）：score={base['score']:.4f}", flush=True)
    else:
        print(f"[0/{total}] 基座分钟回测 ...", flush=True)
        base = _run_score(base_cfg)
        base.update({"combo": "BASE", "where": "", "key": "BASE", "value": None})
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(base, ensure_ascii=False) + "\n")
        print(f"  基座 score={base['score']:.4f} 收益 {_fmt(base['total_return'])} "
              f"做T盈亏 {_fmt(base.get('t_pnl'), pct=False)}", flush=True)

    cnt, skipped = 0, 0
    for where, key, values in grids:
        for v in values:
            combo = f"{where}.{key}={v}"
            cnt += 1
            if combo in done:
                skipped += 1
                continue
            r = _run_score(_apply(base_cfg, {(where, key): v}))
            r.update({"combo": combo, "where": where, "key": key, "value": v})
            done[combo] = r
            with ROWS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[{cnt}/{total}] {combo} -> score={r['score']:.4f} "
                  f"({time.time() - t0:,.0f}s)", flush=True)

    _report(done, base, args)
    print(f"总耗时 {time.time() - t0:,.0f}s（本次实际回测 {cnt - skipped} 次）", flush=True)


def _run_score(cfg: dict) -> dict:
    rep = runner.run_backtest(cfg)
    s = _score(rep)
    m = rep.get("metrics", {}) or {}
    s["total_return"] = m.get("total_return")
    s["max_drawdown"] = m.get("max_drawdown")
    s["t_pnl"] = m.get("t_pnl")
    s["t_pnl_share"] = m.get("t_pnl_share")
    return s


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
        isol = 0.0 if amp <= 1e-9 else \
            (max(scores) - sorted(scores, reverse=True)[1]) / (max(scores) - worst)
        rows.append({"where": where, "key": key, "n": len(rs), "amp": amp,
                     "best_val": best["value"], "best_score": best["score"],
                     "best_vs_base": best["score"] - base_score,
                     "isol": isol, "spike": isol > 0.5})
    rows.sort(key=lambda r: r["amp"], reverse=True)

    lines = [
        "# 做T层 OAT 敏感性普查报告（分钟线，最近四年）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 区间：{args.start} ~ {args.end}｜域 = 2021-06-14 快照随机 {args.subset} 只"
        f"（seed={args.seed}）｜period=minute5｜基座 = HALF_GATE 分钟形态",
        f"- 基座 score = {base_score:.4f}（收益 {_fmt(base['total_return'])}，"
        f"做T盈亏 {_fmt(base.get('t_pnl'), pct=False)}，做T占比 {_fmt(base.get('t_pnl_share'))}）",
        f"- 参数总数 {len(rows)}｜有效组合 {sum(r['n'] for r in rows)} 次",
        "",
        "## 敏感性排名（I(p) = score 极差，降序）",
        "",
        "| # | 参数 | 档位数 | I(p)极差 | 最优档 | 最优score | 相对基座 | 孤立度 | 尖峰 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        tag = f"`{r['where']}.{r['key']}`"
        lines.append(f"| {i} | {tag} | {r['n']} | {r['amp']:.4f} | {r['best_val']} "
                     f"| {r['best_score']:.4f} | {r['best_vs_base']:+.4f} "
                     f"| {r['isol']:.2f} | {'⚠️' if r['spike'] else ''} |")

    pos = [r for r in rows if r["best_vs_base"] > 0.02]
    lines += [
        "",
        "## 判读",
        "",
        f"- 正增量参数（best_vs_base > +0.02）：{len(pos)} 项 —— "
        + (", ".join(f"`{r['where']}.{r['key']}`={r['best_val']}(+{r['best_vs_base']:.3f})"
                     for r in sorted(pos, key=lambda x: -x['best_vs_base'])) or "无"),
        f"- 做T层整体能否翻案（校准首测 -64.57%）：看正增量幅度与基座 score 的差距，"
        f"若全域负增量 → '做T层 zz500 域无效' 定论；若存在 +0.1 以上正增量 → 进做T层砍半/寻优",
        "- 条件参数第二批（reentry_discount 仅 discipline、fwd_t_budget_pct 仅 fwd_t=on、"
        "t_mode 组交互）视本批结果决定",
    ]
    out = OUT_DIR / f"stageT_oat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
