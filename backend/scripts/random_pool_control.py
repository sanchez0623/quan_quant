# -*- coding: utf-8 -*-
"""随机池对照实验（收益归因·实验2）：同参数机制下，真实动量选股 vs 随机等权池。

回答的问题：momentum_slot 的收益率有多少来自选股（底仓方向收益），多少来自
交易机制（做T波动变现）？——把候选域内选股替换为随机等权抽样（池=槽位数，
消除 rank_key 座次挑选空间），机制参数/区间/资金完全一致，重复 K 次取分布；
真实组（universe_auto 动量预筛，zz500 域）跑 1 次作对照。
判定：真实组总收益在随机分布中的分位（percentile rank）+ 两组 t_pnl_share
（做T贡献占比）对比。

用法（backend/ 下）：
  python scripts/random_pool_control.py                       # 默认 2025 全年, K=20, N=3
  python scripts/random_pool_control.py --start 2024-01-01 --end 2025-12-31 --runs 20 --pool-size 3
输出：scripts/out/random_pool_control_<时间戳>.md + 控制台摘要。

口径说明：
- 随机组为静态池（universe=随机 N 只，N=max_holdings 档位时池内无挑选空间），
  无动态重选——这是"选股无关"的极限对照；真实组为 universe_auto 动量预筛，
  两组其余机制（做T/止损/仓位/费率）完全一致。
- 做T收益 t_pnl 为配对毛价差（未扣费，保守）；底仓收益 position_pnl 为残差
  （= 调整口径总盈亏 - t_pnl，承担全部费用）。
- B 双层止损按当前实战口径开启（trade_tier_on，档位用引擎默认），先验参数
  不扫描——避免把实验本身变成过拟合来源。
"""
import argparse
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import store  # noqa: E402
from app.engine import runner  # noqa: E402

INDICES = ["zz500"]
METRIC_KEYS = ["total_return", "max_drawdown", "t_pnl_share",
               "t_pnl", "position_pnl", "t_trade_count", "win_rate"]


def _zz500_universe(data_dir=None) -> list[str]:
    idx = store.read_index_constituents(data_dir)
    if idx is None or idx.height == 0:
        raise RuntimeError("指数成分数据未就绪，请先在数据管理页更新行业与成分")
    codes = sorted(set(idx.filter(pl.col("index_key").is_in(INDICES))["code"].to_list()))
    if len(codes) < 50:
        raise RuntimeError(f"zz500 成分过少（{len(codes)}），数据可疑")
    return codes


def _base_cfg(args, universe: list[str], universe_auto: bool) -> dict:
    return {
        "name": "random_pool_control",
        "strategy_id": "momentum_slot",
        "params": {},                            # schema 默认（先验，不扫描）
        "risk_config": {"trade_tier_on": True},  # B 双层止损：当前实战口径
        "universe": universe,
        "universe_auto": universe_auto,
        "auto_index": INDICES if universe_auto else [],
        "auto_boards": [],
        "start_date": args.start,
        "end_date": args.end,
        "period": "minute5",   # 做T只在 minute5 生效（日线引擎硬关 max_t_times）
        "initial_capital": args.capital,
        "benchmark": "000905",
        "pool_gate": False,
    }


def _metrics_row(m: dict) -> dict:
    return {k: m.get(k) for k in METRIC_KEYS}


def _fmt(v, pct=True):
    if v is None:
        return "-"
    return f"{v:+.2%}" if pct else f"{v:,.0f}"


def _dist(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"mean": None, "median": None, "p5": None, "p95": None, "pos": None}
    q = statistics.quantiles(xs, n=20)
    return {"mean": statistics.mean(xs), "median": statistics.median(xs),
            "p5": q[0], "p95": q[-1],
            "pos": f"{sum(1 for x in xs if x > 0)}/{len(xs)}"}


def _percentile_rank(xs: list, v) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs or v is None:
        return None
    return sum(1 for x in xs if x <= v) / len(xs)


def _verdict(pr: float | None) -> str:
    if pr is None:
        return "分位不可计算（随机组样本不足）"
    if pr >= 0.9:
        return ("真实选股显著跑赢随机池（分位 %.0f%%）→ 收益强依赖选股，"
                "做T/机制只是增强" % (pr * 100))
    if pr >= 0.6:
        return "真实选股有正贡献（分位 %.0f%%）→ 选股主导、机制增强的混合结构" % (pr * 100)
    if pr >= 0.4:
        return ("真实选股与随机池无显著差异（分位 %.0f%%）→ 收益接近与选股无关，"
                "机制主导" % (pr * 100))
    return "随机池反而更好（分位 %.0f%%）→ 该参数下选股为负贡献，机制在烂票上也能T出收益" % (pr * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--pool-size", type=int, default=3, help="随机池大小（默认= max_holdings 档位 3）")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--seed", type=int, default=20260907, help="随机种子（固定保证可复现）")
    args = ap.parse_args()

    t0 = time.time()
    uni_all = _zz500_universe()
    rng = random.Random(args.seed)
    print(f"[1/3] zz500 成分 {len(uni_all)} 只｜区间 {args.start}~{args.end}｜"
          f"随机池 K={args.runs}×N={args.pool_size}｜seed={args.seed}", flush=True)

    rows_rand: list[dict] = []
    for i in range(args.runs):
        uni = sorted(rng.sample(uni_all, args.pool_size))
        cfg = _base_cfg(args, uni, universe_auto=False)
        rep = runner.run_backtest(cfg)
        row = _metrics_row(rep["metrics"])
        row["universe"] = ",".join(uni)
        rows_rand.append(row)
        print(f"  [{i + 1}/{args.runs}] {','.join(uni)} -> "
              f"收益 {_fmt(row['total_return'])}｜T占比 {_fmt(row['t_pnl_share'])}",
              flush=True)

    print("[2/3] 真实组：universe_auto 动量预筛（zz500 域）...", flush=True)
    rep_real = runner.run_backtest(_base_cfg(args, [], universe_auto=True))
    real = _metrics_row(rep_real["metrics"])
    print(f"  真实组 -> 收益 {_fmt(real['total_return'])}｜T占比 {_fmt(real['t_pnl_share'])}",
          flush=True)

    # ---- [3/3] 汇总 ----
    dists = {k: _dist([r[k] for r in rows_rand]) for k in METRIC_KEYS}
    pr_ret = _percentile_rank([r["total_return"] for r in rows_rand], real["total_return"])
    pr_adj = _percentile_rank([r["position_pnl"] for r in rows_rand], real["position_pnl"])

    lines = [
        "# 随机池对照实验（收益归因·实验2）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 区间：{args.start} ~ {args.end}｜初始资金 {args.capital:,.0f}｜"
        f"策略 momentum_slot（schema 默认参数 + B 双层止损 trade_tier_on，先验不扫描）",
        f"- 随机组：zz500 域随机等权 N={args.pool_size}（池=槽位，无挑选空间），"
        f"K={args.runs} 次，seed={args.seed}",
        "- 真实组：universe_auto 动量预筛（zz500 域，auto 默认参数），机制其余完全一致",
        "- 口径：t_pnl=配对毛价差（未扣费，保守）；position_pnl=调整口径总盈亏−t_pnl（残差）",
        "",
        "## 真实组 vs 随机组分布",
        "",
        "| 指标 | 真实组 | 随机均值 | 随机中位 | 随机P5 | 随机P95 | 随机正收益 |",
        "|---|---|---|---|---|---|---|",
    ]
    label = {"total_return": "总收益率", "max_drawdown": "最大回撤",
             "t_pnl_share": "做T收益占比", "t_pnl": "做T盈亏(元)",
             "position_pnl": "底仓盈亏(元)", "t_trade_count": "做T闭环次数",
             "win_rate": "平仓胜率"}
    pct_keys = {"total_return", "max_drawdown", "t_pnl_share", "win_rate"}
    for k in METRIC_KEYS:
        d = dists[k]
        f = (lambda v: _fmt(v)) if k in pct_keys else (lambda v: _fmt(v, pct=False))
        lines.append(f"| {label[k]} | {f(real[k])} | {f(d['mean'])} | {f(d['median'])} "
                     f"| {f(d['p5'])} | {f(d['p95'])} | {d['pos'] or '-'} |")

    lines += [
        "",
        "## 判定",
        "",
        f"- 真实组总收益率在随机分布中的分位：{'-' if pr_ret is None else f'{pr_ret:.0%}'}",
        f"- 真实组底仓盈亏在随机分布中的分位：{'-' if pr_adj is None else f'{pr_adj:.0%}'}",
        f"- **{_verdict(pr_ret)}**",
        "",
        "## 随机组明细（K 次抽样）",
        "",
        "| # | 池 | 总收益率 | 最大回撤 | 做T占比 | 做T盈亏 | 底仓盈亏 | 做T次数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows_rand, 1):
        lines.append(f"| {i} | {r['universe']} | {_fmt(r['total_return'])} "
                     f"| {_fmt(r['max_drawdown'])} | {_fmt(r['t_pnl_share'])} "
                     f"| {_fmt(r['t_pnl'], pct=False)} | {_fmt(r['position_pnl'], pct=False)} "
                     f"| {r['t_trade_count'] or 0} |")

    out = Path(__file__).parent / "out" / \
        f"random_pool_control_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[3/3] 完成（{time.time() - t0:,.0f}s）→ {out}", flush=True)
    print(f"判定：{_verdict(pr_ret)}", flush=True)


if __name__ == "__main__":
    main()
