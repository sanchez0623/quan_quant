# -*- coding: utf-8 -*-
"""阶段 3 半区联合寻优（V3 寻优方案 docs/OPTIMIZE_SLOT_V3_PLAN.md §5.4）。

设计：
- 起点 = HALF_GATE 形态（阶段 2 消融最佳：基座 + 保留名单内 OAT 采纳档 + pool_gate）
- 21 项保留参数分 5 组，Optuna TPE 坐标轮换：2 轮 × 5 组 × 20 trial = 200 次回测
  （round 2 的组外参数用 round 1 各组最优，允许跨组联动）
- 目标 score = 5 窗超额 mean − 0.5×std − dd_floor 击穿罚（与阶段 1/2 同构，三件套内建）
- 每组独立 sqlite study（崩溃续跑：load_if_exists，已完成 trial 不重复）
- 完成后：最优配置跑 全区间 + OOS 段，对比采纳线（HALF_GATE 的 OOS 超额）

用法（backend/ 下）：
  python scripts/stage3_optimize.py --quick      # 每组 3 trial 冒烟
  python scripts/stage3_optimize.py              # 全量 200 trial（~35 分钟）
  python scripts/stage3_optimize.py --auto       # 动态语境 D-寻优（D 版 21 项空间）
输出：scripts/out/stage3_opt_<时间戳>.md + stage3_studies/*.db + 控制台进度。
动态语境（--auto）：起点 = D-消融 HALF_GATE（stageD_oat_rows.jsonl），采纳线
= D-消融 B-OOS 超额 +6.71%；缓存独立命名（stageD_studies / stage3D_rows.jsonl）。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import optuna

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage0_anchors import END_DEFAULT, START_DEFAULT, _cfg, _fmt, _zz500_universe  # noqa: E402
from stage1_oat import _score  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
# 动态语境（--auto）：D-消融起点 + D 版保留空间，缓存与报告独立命名
AUTO = "--auto" in sys.argv
STUDY_DIR = OUT_DIR / ("stageD_studies" if AUTO else "stage3_studies")
ROWS_JSONL = OUT_DIR / ("stage3D_rows.jsonl" if AUTO else "stage3_rows.jsonl")

# 静态版 21 项保留参数（v4 报告），离散档位以 OAT 网格为界
GROUPS_STATIC = [
    ("G1_池与仓位", {
        ("params", "pool_n"): [4, 6, 8, 10, 12, 14, 16, 18],
        ("params", "max_holdings"): [2, 3, 4, 5, 6],
        ("params", "base_pct_max"): [20, 30, 40, 50],
    }),
    ("G2_动量与MACD", {
        ("params", "mom_short"): [5, 10, 15, 20, 25, 30],
        ("params", "macd_slow"): [12, 18, 26, 40, 55],
        ("params", "macd_signal"): [3, 6, 9, 12, 15],
        ("params", "macd_fast"): [5, 8, 12, 16, 20],
    }),
    ("G3_趋势确认与崩溃过滤", {
        ("params", "ma_fast"): [5, 10, 15, 20, 30],
        ("params", "crash_vol_n"): [40, 60, 90, 120],
        ("params", "crash_abs_cap"): [10, 20, 30, 45, 60],
        ("params", "w_accel"): [0.0, 0.1, 0.3, 0.5, 0.7],
        ("params", "w_short"): [0.1, 0.2, 0.3, 0.4, 0.5],
    }),
    ("G4_退出", {
        ("params", "exit_need"): [1, 2, 3],
        ("params", "exit_confirm_days"): [0, 2, 5, 8],
        ("params", "out_top_days"): [0, 1, 2, 3, 5],
        ("params", "add_cooldown"): [1, 3, 5, 10, 15],
    }),
    ("G5_止损与加仓", {
        ("params", "atr_stop_k"): [-6, -5, -4, -3],
        ("risk", "stop_loss_mode"): ["atr", "atr_trailing", "trailing"],
        ("risk", "adaptive"): ["off", "trend", "vol"],
        ("params", "max_adds"): [1, 2, 3, 4],
        ("params", "add_scale"): [0.35, 0.5, 0.65, 0.8],
    }),
]

# 动态语境（D 版）21 项保留参数（stageD_oat_20260912_124506.md 切分），
# 档位以 D-OAT 网格为界（含插值中档）；stop_loss_mode 须含 fixed（D-OAT 反转档）
GROUPS_D = [
    ("G1_池与仓位", {
        ("params", "max_holdings"): [2, 3, 4, 5, 6, 8, 10],
        ("params", "base_pct_max"): [20, 30, 40, 60, 80],
    }),
    ("G2_动量与MACD", {
        ("params", "mom_short"): [5, 10, 15, 20, 25, 30],
        ("params", "mom_long"): [90, 105, 120, 150, 200],
        ("params", "w_mid"): [0.1, 0.2, 0.3, 0.4, 0.5],
        ("params", "macd_fast"): [5, 8, 12, 16, 20],
        ("params", "macd_slow"): [10, 12, 18, 26, 40, 55],
    }),
    ("G3_趋势确认与崩溃过滤", {
        ("params", "w_short"): [0.1, 0.2, 0.3, 0.4, 0.5],
        ("params", "w_accel"): [0.0, 0.1, 0.3, 0.5, 0.7],
        ("params", "ma_fast"): [5, 10, 15, 20, 30, 45],
        ("params", "crash_vol_n"): [20, 40, 60, 90, 120],
        ("params", "crash_sigma"): [1.0, 1.5, 2.0, 2.5, 3.0],
    }),
    ("G4_退出与止盈", {
        ("params", "exit_confirm_days"): [0, 2, 5, 8, 10],
        ("params", "exit_cooldown"): [0, 2, 5, 10, 20],
        ("risk", "take_profit_pct"): [15, 25, 40, 60, 100],
        ("params", "add_cooldown"): [1, 3, 5, 10, 15],
    }),
    ("G5_止损与加仓", {
        ("risk", "atr_multiplier"): [1.5, 2.0, 2.5, 3.0, 3.5],
        ("risk", "stop_loss_mode"): ["fixed", "atr", "atr_trailing", "trailing"],
        ("params", "max_adds"): [0, 1, 2, 3, 4],
        ("params", "add_scale"): [0.2, 0.35, 0.5, 0.65, 0.8],
        ("params", "add_breakout_n"): [5, 10, 20, 35, 55],
    }),
]
GROUPS = GROUPS_D if AUTO else GROUPS_STATIC


def _load_half_gate_overrides() -> dict:
    """重建 HALF_GATE：基座 + 保留名单内 OAT 采纳档 + pool_gate（阶段 2 最佳形态）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import stage2_ablation as s2
    full_ov, half_ov = s2._load_best_overrides()
    ov = dict(half_ov)
    ov[("top", "pool_gate")] = True
    return ov


def _apply(cfg: dict, overrides: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    for (where, key), v in overrides.items():
        if where == "params":
            out["params"][key] = v
        elif where == "risk":
            out["risk_config"][key] = v
        else:
            out[key] = v
    return out


def _ov_from_json(s: str) -> dict:
    """user_attrs 的 JSON（key 形如 'params.pool_n'）-> tuple-key dict。"""
    out = {}
    for k, v in json.loads(s).items():
        where, key = k.split(".", 1)
        out[(where, key)] = v
    return out


def _run_score(cfg: dict) -> dict:
    rep = runner.run_backtest(cfg)
    s = _score(rep)
    m = rep.get("metrics", {}) or {}
    s["total_return"] = m.get("total_return")
    s["max_drawdown"] = m.get("max_drawdown")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=END_DEFAULT)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--trials", type=int, default=20, help="每组每轮 trial 数")
    ap.add_argument("--quick", action="store_true", help="每组每轮 3 trial 冒烟")
    ap.add_argument("--auto", action="store_true",
                    help="动态换血语境（universe_auto=zz500），缓存与报告独立命名")
    args = ap.parse_args()
    n_trials = 3 if args.quick else args.trials

    t0 = time.time()
    OUT_DIR.mkdir(exist_ok=True)
    STUDY_DIR.mkdir(exist_ok=True)
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    uni_all = _zz500_universe(as_of=args.start)
    base_cfg = _cfg("stage3_base", [] if AUTO else uni_all, universe_auto=AUTO,
                    start=args.start, end=args.end, capital=args.capital)
    half_gate = _load_half_gate_overrides()
    base = _apply(base_cfg, half_gate)
    print(f"HALF_GATE 起点：{len(half_gate)} 项采纳（含 gate）", flush=True)

    # 各组当前最优（round1 起点 = HALF_GATE 默认；round2 用 round1 结果）
    group_best: dict[str, dict] = {}   # group -> {(where,key): value}
    group_best_score: dict[str, float] = {}
    best_score = _run_score(base)["score"]
    print(f"HALF_GATE 起点 score = {best_score:.4f}", flush=True)
    with ROWS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"combo": "BASE_HALF_GATE", "score": best_score},
                           ensure_ascii=False) + "\n")

    total = len(GROUPS) * 2 * n_trials
    cnt = 0
    for rnd in (1, 2):
        for gi, (gname, space) in enumerate(GROUPS):
            # 组外参数：round1 = HALF_GATE；round2 = 各组 round1 最优的合成
            outer = dict(half_gate)
            if rnd == 2:
                for gb in group_best.values():
                    outer.update(gb)
            study_name = f"r{rnd}_g{gi}"
            study = optuna.create_study(
                study_name=study_name, direction="maximize",
                storage=f"sqlite:///{STUDY_DIR / (study_name + '.db')}",
                load_if_exists=True)
            done = sum(1 for t in study.trials
                       if t.state == optuna.trial.TrialState.COMPLETE)
            if done >= n_trials:
                print(f"[r{rnd} {gname}] 已有 {done} trial（缓存跳过）", flush=True)
                best = study.best_trial
                group_best[gname] = _ov_from_json(best.user_attrs["ov"])
                group_best_score[gname] = best.value
                cnt += done
                continue

            def objective(trial: optuna.Trial) -> float:
                ov = {}
                for (where, key), choices in space.items():
                    v = trial.suggest_categorical(f"{where}.{key}", choices)
                    ov[(where, key)] = v
                cfg = _apply(base, {**outer, **ov})
                try:
                    r = _run_score(cfg)
                except Exception as e:  # 退化组合（如动态初始池筛空）记最低分不终止
                    print(f"  [r{rnd} {gname}] trial{trial.number} 退化：{e}",
                          flush=True)
                    r = {"score": -9e9, "mean_excess": None, "std_excess": None,
                         "min_dd": None, "windows": 0,
                         "total_return": None, "max_drawdown": None}
                trial.set_user_attr("ov", json.dumps(
                    {f"{w}.{k}": v for (w, k), v in ov.items()}))
                trial.set_user_attr("total_return", r["total_return"])
                trial.set_user_attr("max_drawdown", r["max_drawdown"])
                with ROWS_JSONL.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "round": rnd, "group": gname, "combo": trial.number,
                        "params": {f"{w}.{k}": v for (w, k), v in ov.items()},
                        **{k: v for k, v in r.items()}}, ensure_ascii=False) + "\n")
                return r["score"]

            study.optimize(objective, n_trials=n_trials - done)
            best = study.best_trial
            group_best[gname] = _ov_from_json(best.user_attrs["ov"])
            group_best_score[gname] = best.value
            if best.value > best_score:
                pass  # 组最优在轮内记录，轮末统一合成
            print(f"[r{rnd} {gname}] best score={best.value:.4f} "
                  f"({best.user_attrs.get('ov', '')[:120]})", flush=True)
            cnt += n_trials

    # 合成最终配置：HALF_GATE + 各组最优
    final_ov = dict(half_gate)
    for gb in group_best.values():
        final_ov.update(gb)
    final_cfg = _apply(base_cfg, final_ov)
    split_rep = runner.run_backtest(_cfg("stage3_split_probe", [] if AUTO else uni_all,
                                         universe_auto=AUTO,
                                         start=args.start, end=args.end,
                                         capital=args.capital))
    dates = sorted({str(p.get("date"))[:10]
                    for p in split_rep.get("equity_curve") or []})
    split = dates[int(len(dates) * 0.7) - 1]

    oos_cfg = _cfg("stage3_best_oos", [] if AUTO else uni_all, universe_auto=AUTO,
                   start=split, end=args.end, capital=args.capital)
    oos_cfg = _apply(oos_cfg, final_ov)
    final_cfg["name"] = "stage3_best"
    merged_mode = "全部组最优"
    print("[final] 最优合成配置：全区间 + OOS ...", flush=True)
    try:
        rep = runner.run_backtest(final_cfg)
        rep_oos = runner.run_backtest(oos_cfg)
    except Exception as e:
        # 联合配置退化（如动态初始池筛空）→ 仅并入优于起点的组重试
        print(f"[final] 联合配置退化（{e}）→ 仅并入优于起点的组重试", flush=True)
        final_ov = dict(half_gate)
        for gname, gb in group_best.items():
            if group_best_score.get(gname, -9e9) > best_score:
                final_ov.update(gb)
        final_cfg = _apply(base_cfg, final_ov)
        final_cfg["name"] = "stage3_best"
        oos_cfg = _apply(_cfg("stage3_best_oos", [] if AUTO else uni_all,
                              universe_auto=AUTO, start=split, end=args.end,
                              capital=args.capital), final_ov)
        merged_mode = "仅并入优于起点的组（联合配置退化回退）"
        rep = runner.run_backtest(final_cfg)
        rep_oos = runner.run_backtest(oos_cfg)
    m = rep.get("metrics", {}) or {}
    final_full = {k: m.get(k) for k in
                  ("total_return", "annual_return", "benchmark_return",
                   "excess_return", "max_drawdown", "sharpe", "win_rate")}
    m_oos = rep_oos.get("metrics", {}) or {}
    final_oos = {k: m_oos.get(k) for k in
                 ("total_return", "annual_return", "benchmark_return",
                  "excess_return", "max_drawdown", "sharpe", "win_rate")}

    label = "动态语境 universe_auto(zz500)" if AUTO else "静态池"
    accept_th = 0.0671 if AUTO else 0.0410
    accept_src = ("D-消融 B-OOS 超额 +6.71%" if AUTO else "阶段 2 消融")
    lines = [
        "# 阶段 3 联合寻优报告（动态语境）" if AUTO else "# 阶段 3 联合寻优报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 区间：{args.start} ~ {args.end}｜OOS = {split} 起｜起点 = HALF_GATE"
        f"｜语境 = {label}｜5 组 × 2 轮 × {n_trials} trial",
        f"- 合成口径：{merged_mode}",
        "",
        "## 最优配置（HALF_GATE + 各组最优）",
        "",
        "```json",
        json.dumps({f"{w}.{k}": v for (w, k), v in sorted(final_ov.items())},
                   ensure_ascii=False, indent=1),
        "```",
        "",
        "| 口径 | 总收益 | 年化 | 基准 | 超额 | 回撤 | 夏普 | 胜率 |",
        "|---|---|---|---|---|---|---|---|",
        f"| 全区间 | {_fmt(final_full['total_return'])} | {_fmt(final_full['annual_return'])} "
        f"| {_fmt(final_full['benchmark_return'])} | {_fmt(final_full['excess_return'])} "
        f"| {_fmt(final_full['max_drawdown'])} | {_fmt(final_full['sharpe'], pct=False)} "
        f"| {_fmt(final_full['win_rate'])} |",
        f"| OOS | {_fmt(final_oos['total_return'])} | {_fmt(final_oos['annual_return'])} "
        f"| {_fmt(final_oos['benchmark_return'])} | {_fmt(final_oos['excess_return'])} "
        f"| {_fmt(final_oos['max_drawdown'])} | {_fmt(final_oos['sharpe'], pct=False)} "
        f"| {_fmt(final_oos['win_rate'])} |",
        "",
        "## 采纳判定（对照方案 §5.4）",
        "",
        f"- 采纳线：HALF_GATE 的 OOS 超额 {accept_th:.2%}（{accept_src}）",
        f"- 寻优最优 OOS 超额：{_fmt(final_oos['excess_return'])}"
        f"（{'✅ 赢采纳线，可采纳' if (final_oos['excess_return'] or -9) > accept_th else '❌ 未过采纳线，参数不采纳'}）",
        f"- D3① 标准档：全区间总收益 vs 基准 {_fmt(final_full['total_return'])} vs "
        f"{_fmt(final_full['benchmark_return'])}，年化超额需 ≥ 8%",
    ]
    prefix = "stageD_opt" if AUTO else "stage3_opt"
    out = OUT_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)
    print(f"总耗时 {time.time() - t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
