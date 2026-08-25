# -*- coding: utf-8 -*-
"""绩效统计：核心指标 + 做T/加减仓贡献分解 + 月度收益"""
from typing import Optional


def _safe_div(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def monthly_returns(equity_curve: list[dict], start_equity: float) -> list[dict]:
    """equity_curve 按月末采样计算月度收益"""
    out: list[dict] = []
    last_by_month: dict[tuple[int, int], tuple[str, float]] = {}
    for p in equity_curve:
        y, m = int(p["date"][:4]), int(p["date"][5:7])
        last_by_month[(y, m)] = (p["date"], p["equity"])
    prev_equity = start_equity
    for (y, m) in sorted(last_by_month):
        _d, eq = last_by_month[(y, m)]
        ret = eq / prev_equity - 1 if prev_equity else 0.0
        out.append({"year": y, "month": m, "return": round(ret, 6)})
        prev_equity = eq
    return out


def build_metrics(trade_log: list[dict], equity_curve: list[dict],
                  start_equity: float, end_equity: float,
                  commission_total: float,
                  t_cycle_pnls: Optional[list[float]] = None) -> dict:
    """契约 metrics 全字段 + 做T贡献分解"""
    # ---- 收益/风险（基于日频 equity 曲线）----
    total_return = end_equity / start_equity - 1 if start_equity else 0.0
    n_days = len(equity_curve)
    annual_return = (end_equity / start_equity) ** (252 / max(n_days, 1)) - 1 \
        if start_equity > 0 and end_equity > 0 and n_days > 0 else 0.0

    max_drawdown = 0.0
    peak = None
    rets: list[float] = []
    prev_eq = None
    for p in equity_curve:
        eq = p["equity"]
        if prev_eq is not None and prev_eq > 0:
            rets.append(eq / prev_eq - 1)
        prev_eq = eq
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            max_drawdown = min(max_drawdown, eq / peak - 1)

    def _sharpe() -> Optional[float]:
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = var ** 0.5
        if std == 0:
            return None
        return mean / std * (252 ** 0.5)

    def _sortino() -> Optional[float]:
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        downside = [r for r in rets if r < 0]
        if not downside:
            return None
        dvar = sum(r * r for r in downside) / len(downside)
        dstd = dvar ** 0.5
        if dstd == 0:
            return None
        return mean / dstd * (252 ** 0.5)

    sharpe = _sharpe()
    sortino = _sortino()
    calmar = _safe_div(annual_return, abs(max_drawdown)) if max_drawdown < 0 else None

    # ---- 平仓笔统计（做T分解：持有时长<1交易日 → T交易）----
    closes = [t for t in trade_log if t["side"] == "sell" and t.get("pnl") is not None]
    wins = [t for t in closes if t["pnl"] > 0]
    losses = [t for t in closes if t["pnl"] < 0]
    win_rate = _safe_div(len(wins), len(closes))
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    plr = _safe_div(avg_win, abs(avg_loss)) if avg_loss != 0 else None

    hold_days: list[float] = []
    t_pnl = 0.0
    t_count = 0
    t_wins = 0
    open_pnl = add_pnl = reduce_pnl = stop_pnl = 0.0
    if t_cycle_pnls:
        # 做T贡献 = 已完成"卖旧买新"周期的价差（跨日持续至还清，独立于平仓盈亏口径）
        t_count = len(t_cycle_pnls)
        t_pnl = sum(t_cycle_pnls)
        t_wins = len([p for p in t_cycle_pnls if p > 0])
    for t in closes:
        pnl = t["pnl"]
        open_day = (t.get("open_time") or t["time"])[:10]
        close_day = t["time"][:10]
        is_t = open_day == close_day  # 当日买当日卖 → T交易（T+1下罕见）
        tag = t.get("tag") or "开仓"
        if is_t and not t_cycle_pnls:
            t_count += 1
            t_pnl += pnl
            if pnl > 0:
                t_wins += 1
        elif t["type"] == "止损":
            stop_pnl += pnl
        elif t["type"] == "减仓":
            reduce_pnl += pnl
        elif tag == "加仓":
            add_pnl += pnl
        else:
            # 做T买回的仓位本质是重新取得的底仓，其平仓盈亏归入持仓贡献
            open_pnl += pnl
        # 持仓天数（自然日）
        try:
            from datetime import datetime
            d1 = datetime.strptime(open_day, "%Y-%m-%d")
            d2 = datetime.strptime(close_day, "%Y-%m-%d")
            hold_days.append(max(0.0, (d2 - d1).days))
        except ValueError:
            pass

    total_pnl = sum(t["pnl"] for t in closes)
    avg_hold = _safe_div(sum(hold_days), len(hold_days)) if hold_days else 0.0

    return {
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sortino": round(sortino, 4) if sortino is not None else None,
        "calmar": round(calmar, 4) if calmar is not None else None,
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "profit_loss_ratio": round(plr, 4) if plr is not None else None,
        "total_trades": len(trade_log),
        "total_pnl": round(total_pnl, 2),
        "avg_hold_days": round(avg_hold, 2),
        "t_trade_count": t_count,
        "t_win_rate": round(t_wins / t_count, 6) if t_count else None,
        "t_pnl": round(t_pnl, 2),
        "open_pnl": round(open_pnl, 2),
        "add_pnl": round(add_pnl, 2),
        "reduce_pnl": round(reduce_pnl, 2),
        "stop_loss_pnl": round(stop_pnl, 2),
        "commission_total": round(commission_total, 2),
        "start_equity": round(start_equity, 2),
        "end_equity": round(end_equity, 2),
    }
