# -*- coding: utf-8 -*-
"""大盘趋势闸门 A/B 对照实验（INDEX_GATE 验证）：同配置 index_gate on vs off。

回答的问题：中证500 收盘跌破 MA20 连续 2 日时停开新仓（退出与做T照常），
对年化收益与最大回撤的净效应是多少？——按年度窗口切分（分钟数据 2023-04 起），
每年 on/off 各跑一次，其余配置完全一致，看差值在不同市况下的稳定性。

用法（backend/ 下）：
  python scripts/index_gate_ab.py                     # 默认 2023-04 起 4 个年度窗口
  python scripts/index_gate_ab.py --start 2024-01-01 --end 2025-12-31
输出：scripts/out/index_gate_ab_<时间戳>.md + 控制台摘要。

口径说明：
- momentum_slot + universe_auto（zz500 域动量预筛）+ minute5 + B 双层止损
  （trade_tier_on，引擎默认档），schema 默认参数（先验，不扫描）；
- pool_gate=False：隔离大盘闸门自身的效应（池级开关正交，不混入对照）；
- gate_on_days = 窗口内闸门生效交易日数（样本有效性：占比过低则该窗口
  on/off 差异无意义）。
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import momentum_core as mc  # noqa: E402
from app.engine import runner  # noqa: E402

METRICS = [
    ("total_return", "总收益率", True),
    ("annual_return", "年化收益", True),
    ("max_drawdown", "最大回撤", True),
    ("sharpe", "夏普", False),
    ("calmar", "卡玛", False),
    ("excess_return", "超额(vs中证500)", True),
    ("total_trades", "成交笔数", False),
    ("opens", "开仓次数", False),
    ("t_trade_count", "做T闭环次数", False),
    ("t_pnl", "做T盈亏(元)", False),
    ("win_rate", "平仓胜率", True),
    ("commission_total", "佣金合计(元)", False),
]

DEFAULT_WINDOWS = [
    ("2023-04-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
    ("2026-01-01", "2026-09-04"),
]


def _fmt(v, pct=True):
    if v is None:
        return "-"
    return f"{v:+.2%}" if pct else f"{v:,.2f}"


def _gate_segments(start: str, end: str) -> tuple[int, list[tuple[str, str]]]:
    """窗口内闸门生效交易日数 + 触发段列表"""
    gate = mc.compute_index_gate()
    days = gate["day"].to_list()
    vals = gate["index_gate"].to_list()
    segs: list[list[str]] = []
    on_days = 0
    prev = False
    for day, v in zip(days, vals):
        if day < start or day > end:
            continue
        if v:
            on_days += 1
            if not prev:
                segs.append([day, day])
            else:
                segs[-1][1] = day
        prev = v
    return on_days, [tuple(s) for s in segs]


def _base_cfg(capital: float, start: str, end: str) -> dict:
    return {
        "name": "index_gate_ab",
        "strategy_id": "momentum_slot",
        "params": {},                            # schema 默认（先验，不扫描）
        "risk_config": {"trade_tier_on": True},  # B 双层止损：当前实战口径
        "universe": [],
        "universe_auto": True,
        "auto_index": ["zz500"],
        "auto_boards": [],
        "start_date": start,
        "end_date": end,
        "period": "minute5",
        "initial_capital": capital,
        "benchmark": "000905",
        "pool_gate": False,
    }


def _run_one(capital: float, start: str, end: str, index_gate: bool) -> dict:
    cfg = _base_cfg(capital, start, end)
    cfg["index_gate"] = index_gate
    rep = runner.run_backtest(cfg)
    m = dict(rep["metrics"])
    m["opens"] = sum(1 for t in rep.get("trade_log", []) if t.get("type") == "开仓")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="自定义单一窗口起点（覆盖默认年度窗口）")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    args = ap.parse_args()

    windows = ([(args.start, args.end)] if args.start and args.end
               else DEFAULT_WINDOWS)
    t0 = time.time()
    print(f"INDEX_GATE A/B：{len(windows)} 个窗口 × on/off｜"
          f"momentum_slot + universe_auto(zz500) + minute5｜资金 {args.capital:,.0f}",
          flush=True)

    results = []  # (窗口, off_metrics, on_metrics, on_days, segs)
    for wi, (start, end) in enumerate(windows, 1):
        on_days, segs = _gate_segments(start, end)
        print(f"[{wi}/{len(windows)}] {start}~{end}｜闸门生效 {on_days} 日｜"
              f"触发段 {len(segs)} 个", flush=True)
        print("  off ...", flush=True)
        m_off = _run_one(args.capital, start, end, False)
        print(f"  off -> 收益 {_fmt(m_off['total_return'])}｜回撤 {_fmt(m_off['max_drawdown'])}",
              flush=True)
        print("  on  ...", flush=True)
        m_on = _run_one(args.capital, start, end, True)
        print(f"  on  -> 收益 {_fmt(m_on['total_return'])}｜回撤 {_fmt(m_on['max_drawdown'])}",
              flush=True)
        results.append((start, end, m_off, m_on, on_days, segs))

    # ---- 汇总 ----
    lines = [
        "# 大盘趋势闸门 A/B 对照（INDEX_GATE）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 配置：momentum_slot + universe_auto（zz500 域动量预筛）+ minute5 + "
        f"B双层止损（trade_tier_on，默认档）｜schema 默认参数（先验不扫描）｜"
        f"资金 {args.capital:,.0f}｜pool_gate=False（隔离闸门效应）",
        "- 对照：同窗口 index_gate off vs on，其余完全一致",
        "",
        "## 分窗口对照矩阵",
        "",
        "| 窗口 | 闸门生效 | 指标 | off | on | 差值(on-off) |",
        "|---|---|---|---|---|---|",
    ]
    for start, end, m_off, m_on, on_days, _segs in results:
        for j, (key, label, pct) in enumerate(METRICS):
            v_off, v_on = m_off.get(key), m_on.get(key)
            diff = (v_on - v_off) if (v_off is not None and v_on is not None) else None
            f = (lambda v: _fmt(v)) if pct else (lambda v: _fmt(v, pct=False))
            win = f"{start}~{end}（生效 {on_days} 日）" if j == 0 else ""
            lines.append(f"| {win} | | {label} | {f(v_off)} | {f(v_on)} | {f(diff)} |")

    lines += ["", "## 各窗口闸门触发段", ""]
    for start, end, _o, _n, on_days, segs in results:
        lines.append(f"- **{start}~{end}**：生效 {on_days} 日"
                     + ("" if not segs else "｜" + "、".join(f"{s}~{e}" for s, e in segs)))

    lines += [
        "",
        "## 判定要点",
        "",
        "- 回撤维度的差值是闸门的主要设计目标（下跌段停开新仓）；收益差值为代价或改善",
        "- 触发占比过低的窗口（<20%），on/off 差异主要来自随机路径，不宜过度解读",
        "- 差值在多窗口方向一致才可视为稳健结论；单窗口改善不足为凭",
    ]

    out = Path(__file__).parent / "out" / \
        f"index_gate_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"完成（{time.time() - t0:,.0f}s）→ {out}", flush=True)


if __name__ == "__main__":
    main()
