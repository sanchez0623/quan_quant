# -*- coding: utf-8 -*-
"""阶段 0 锚点回测（V3 寻优方案 docs/OPTIMIZE_SLOT_V3_PLAN.md §5.1）：BT-D1~D4 四锚点。

目的：在历史中性原则下建立本轮新基线，回答四个问题：
- BT-D1  静态 500 池 + L2 参数        -> 扩域后基座离中证500 多远（骨架寻优采纳线）
- BT-D2  BT-D1 + pool_gate            -> 池级趋势开关在长区间的增量
- BT-D3  zz500 域随机 300 子集 + L2    -> 池内选股 luck 的方差量级（跨池验收预检）
- BT-D4  universe_auto(['zz500'])+L2  -> 动态换血 vs 静态池直接对照（核心假设）

用法（backend/ 下）：
  python scripts/stage0_anchors.py --smoke   # 3 个月小区间只跑 BT-D1，验证配置
  python scripts/stage0_anchors.py           # 全区间跑 BT-D1~D4
输出：scripts/out/stage0_anchors_<时间戳>.md + 控制台摘要。

口径：
- period=daily（日线引擎硬关做T层）；区间 2023-03-27~2026-09-07（zz500 日线批量起点）；
  初始资金 300 万（对齐 RESEARCH 基座）；基准 000905；剔除 ST。
- L2 参数 = RESEARCH_MOMENTUM_SLOT.md §7 当前正式参数，仅作操作起点（历史中性，不背书历史判断）。
- 超额 = runner 内建 excess_return（total_return - benchmark_return，算术口径）。
"""
import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import store  # noqa: E402
from app.engine import runner  # noqa: E402

START_DEFAULT = "2022-09-11"   # v5 重验区间（用户指定 2022.9.11-2026.9.10，与实盘
                               # 任务 bt_e2b0ca48c46d 同期）；预热充足，快照沿用 2021-06-14
END_DEFAULT = "2026-09-10"
BENCHMARK = "000905"
SEED_DEFAULT = 20260908

L2_PARAMS = {
    "mom_short": 15, "mom_mid": 90, "w_short": 0.3, "w_mid": 0.3,
    "pool_n": 6, "max_holdings": 6, "base_pct_max": 45, "base_pct_min": 10,
}
L2_RISK = {"max_position_pct_per_stock": 35}
AUTO_DEFAULTS = {
    "auto_top_x": 30, "auto_idle_days": 5, "auto_above_ma": 20,
    "auto_with_accel": True, "auto_rank_key": "score",
}

METRIC_KEYS = ["total_return", "annual_return", "benchmark_return", "excess_return",
               "max_drawdown", "sharpe", "calmar", "win_rate"]


def _zz500_universe(as_of: str) -> list[str]:
    """候选域 = snap_date <= 回测起点(as_of) 的最近 zz500 历史快照（静态池完全无后视）。

    勘误史（2026-09-08）：
    - v1（作废）：当前成分快照回测历史区间，混入 239 只未来调入股（成分前视）；
    - v2（作废）：硬编码 2023-03-27 快照，但回测起点 2021-01-04 早于快照日，
      2021~2023.03 段仍有残余偏差（该快照与 2020-12-28 快照相差 332 只）；
    - v3（本版）：快照日随回测起点动态取（<= start 最近一期），起点=快照日后
      数个交易日内，静态池与后端 _auto_domain 的 as_of 语义对齐。"""
    hist_path = Path(__file__).resolve().parents[2] / "data" / \
        "index_constituents_history.parquet"
    hist = pl.read_parquet(hist_path)
    hz = hist.filter((pl.col("index_key") == "zz500") & (pl.col("snap_date") <= as_of))
    if hz.height == 0:
        raise RuntimeError(f"历史成分无 <= {as_of} 的快照")
    snap = hz["snap_date"].max()
    codes = sorted(set(hz.filter(pl.col("snap_date") == snap)["code"].to_list()))
    print(f"成分域：zz500 历史快照 {snap}（{len(codes)} 只，<=起点最近一期，无后视）",
          flush=True)
    if len(codes) < 400:
        raise RuntimeError(f"zz500 成分过少（{len(codes)}），数据可疑")
    return codes


def _cfg(name: str, universe: list[str], *, universe_auto: bool = False,
         pool_gate: bool = False, start: str = START_DEFAULT,
         end: str = END_DEFAULT, capital: float = 3_000_000.0) -> dict:
    cfg = {
        "name": name,
        "strategy_id": "momentum_slot",
        "params": dict(L2_PARAMS),
        "risk_config": dict(L2_RISK),
        "universe": [] if universe_auto else universe,
        "universe_auto": universe_auto,
        "auto_index": ["zz500"] if universe_auto else [],
        "auto_boards": [],
        "start_date": start,
        "end_date": end,
        "period": "daily",
        "initial_capital": capital,
        "benchmark": BENCHMARK,
        "pool_gate": pool_gate,
        "pool_gate_enter_th": 0.15,
        "exclude_st": True,
    }
    if universe_auto:
        cfg.update(AUTO_DEFAULTS)
    return cfg


def _fmt(v, pct=True):
    if v is None:
        return "-"
    if pct:
        return f"{v:+.2%}"
    return f"{v:,.2f}"


def _run_one(tag: str, cfg: dict, rows: list[dict]) -> None:
    t0 = time.time()
    rep = runner.run_backtest(cfg)
    m = rep.get("metrics", {}) or {}
    row = {"tag": tag, "name": cfg["name"]}
    row.update({k: m.get(k) for k in METRIC_KEYS})
    row["bench_covered"] = bool(rep.get("benchmark"))
    rows.append(row)
    print(f"  [{tag}] {time.time() - t0:,.0f}s 收益 {_fmt(row['total_return'])} "
          f"| 基准 {_fmt(row['benchmark_return'])} | 超额 {_fmt(row['excess_return'])} "
          f"| 回撤 {_fmt(row['max_drawdown'])} | 夏普 {_fmt(row['sharpe'], pct=False)}",
          flush=True)


CONF_LABEL = {
    "BT-D1": "静态500 + L2",
    "BT-D2": "静态500 + L2 + pool_gate",
    "BT-D3": "随机300(seed) + L2",
    "BT-D4": "universe_auto(zz500) + L2",
}


def _write(rows: list[dict], args, smoke: bool) -> None:
    label = {"total_return": "总收益率", "annual_return": "年化",
             "benchmark_return": "基准000905", "excess_return": "超额",
             "max_drawdown": "最大回撤", "sharpe": "夏普", "calmar": "卡玛",
             "win_rate": "平仓胜率"}
    pct_keys = {"total_return", "annual_return", "benchmark_return", "excess_return",
                "max_drawdown", "win_rate"}
    rng_txt = "2023-04-01 ~ 2023-06-30（SMOKE）" if smoke else f"{args.start} ~ {args.end}"
    lines = [
        "# 阶段 0 锚点回测（BT-D1~D4）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 区间：{rng_txt}｜资金 {args.capital:,.0f}｜日线｜基准 000905｜剔除ST"
        f"｜D3 抽样 seed={args.seed}",
        "- L2 起点：mom_short 15 / mom_mid 90 / w_short 0.3 / w_mid 0.3 / pool_n 6 / "
        "max_holdings 6 / base_pct_max 45 / base_pct_min 10 / 单票上限 35%；"
        "风控默认 stop_loss_mode=atr_trailing（仅操作起点，历史中性）",
        "",
        "| 锚点 | 配置 | " + " | ".join(label[k] for k in METRIC_KEYS) + " |",
        "|---|---|" + "---|" * len(METRIC_KEYS),
    ]
    for r in rows:
        cells = []
        for k in METRIC_KEYS:
            v = r.get(k)
            cells.append(_fmt(v) if k in pct_keys else _fmt(v, pct=False))
        lines.append(f"| {r['tag']} | {CONF_LABEL.get(r['tag'], r['name'])} | "
                     + " | ".join(cells) + " |")

    by_tag = {r["tag"]: r for r in rows}
    d1, d2, d3, d4 = (by_tag.get(t) for t in ("BT-D1", "BT-D2", "BT-D3", "BT-D4"))
    lines += ["", "## 判读（对照方案 §5.1）", ""]
    if d1 and d1["total_return"] is not None:
        lines.append(f"- **BT-D1（骨架采纳线）**：总收益 {_fmt(d1['total_return'])} vs 基准 "
                     f"{_fmt(d1['benchmark_return'])}，超额 {_fmt(d1['excess_return'])}，"
                     f"回撤 {_fmt(d1['max_drawdown'])}——后续寻优产物赢不了此锚即不采纳")
    if d2 and d1 and None not in (d2["total_return"], d1["total_return"]):
        lines.append(f"- **BT-D2 gate 增量**：收益差 "
                     f"{_fmt(d2['total_return'] - d1['total_return'])}，"
                     f"回撤 {_fmt(d2['max_drawdown'])} vs {_fmt(d1['max_drawdown'])}")
    if d3 and d1 and None not in (d3["total_return"], d1["total_return"]):
        lines.append(f"- **BT-D3 池内 luck 预检**：随机300 收益 {_fmt(d3['total_return'])} "
                     f"vs 全500 {_fmt(d1['total_return'])}，差 "
                     f"{_fmt(d3['total_return'] - d1['total_return'])}——该量级即跨池验收的本底噪声")
    if d4 and d1 and None not in (d4["total_return"], d1["total_return"]):
        lines.append(f"- **BT-D4 动态换血 vs 静态（核心假设）**：收益差 "
                     f"{_fmt(d4['total_return'] - d1['total_return'])}"
                     f"（D4 {_fmt(d4['total_return'])} / D1 {_fmt(d1['total_return'])}）"
                     f"——历史 BT-B 否定结论不作先验，以此轮数据为准")

    out = Path(__file__).parent / "out" / \
        f"stage0_anchors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"完成 → {out}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=END_DEFAULT)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--seed", type=int, default=SEED_DEFAULT)
    ap.add_argument("--smoke", action="store_true",
                    help="3 个月小区间只跑 BT-D1，验证配置")
    args = ap.parse_args()

    t0 = time.time()
    start, end = ("2021-02-01", "2021-04-30") if args.smoke else (args.start, args.end)
    uni_all = _zz500_universe(as_of=start)
    rng = random.Random(args.seed)
    uni_300 = sorted(rng.sample(uni_all, 300))
    kw = dict(start=start, end=end, capital=args.capital)
    print(f"zz500 成分 {len(uni_all)} 只｜区间 {start}~{end}｜资金 {args.capital:,.0f}｜"
          f"seed={args.seed}{'｜SMOKE' if args.smoke else ''}", flush=True)

    rows: list[dict] = []
    print("[1/4] BT-D1 静态500 + L2 ...", flush=True)
    _run_one("BT-D1", _cfg("stage0_D1_static500_L2", uni_all, **kw), rows)
    if args.smoke:
        _write(rows, args, smoke=True)
        return
    print("[2/4] BT-D2 静态500 + L2 + pool_gate ...", flush=True)
    _run_one("BT-D2", _cfg("stage0_D2_static500_gate", uni_all, pool_gate=True, **kw), rows)
    print("[3/4] BT-D3 随机300 + L2 ...", flush=True)
    _run_one("BT-D3", _cfg("stage0_D3_random300_L2", uni_300, **kw), rows)
    print("[4/4] BT-D4 universe_auto(zz500) + L2 ...", flush=True)
    _run_one("BT-D4", _cfg("stage0_D4_auto_zz500_L2", [], universe_auto=True, **kw), rows)
    _write(rows, args, smoke=False)
    print(f"总耗时 {time.time() - t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
