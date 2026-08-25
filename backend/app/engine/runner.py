# -*- coding: utf-8 -*-
"""回测主流程：向量化信号 → 逐bar撮合（T+1、涨跌停、滑点、手续费、分层持仓、风控）
输出契约 report 结构：metrics / equity_curve / monthly_returns / trade_log / position_snapshots
"""
from datetime import datetime
from typing import Callable, Optional

import polars as pl

from . import datafeed
from .broker import Broker
from .indicators import add_atr
from .portfolio import Portfolio
from .risk import RiskConfig, RiskManager
from .stats import build_metrics, monthly_returns
from .strategies import REGISTRY, apply_param_defaults
from ..data import store

DEFAULTS = {
    "initial_capital": 1_000_000.0,
    "slippage_pct": 0.001,
    "commission_rate": 0.0003,
    "commission_min": 5.0,
    "stamp_tax": 0.001,
    "transfer_fee": 0.00001,
    "exclude_st": True,
}


def run_backtest(config: dict, data_dir: Optional[str] = None,
                 progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    """config: 契约 POST /api/backtests 请求体（params 已填默认值）。返回完整 report dict。"""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if v is not None})
    strategy_id = cfg["strategy_id"]
    strategy = REGISTRY[strategy_id]
    params = apply_param_defaults(strategy_id, cfg.get("params") or {})
    period = cfg.get("period", "daily")
    universe = list(cfg.get("universe") or [])
    start, end = cfg.get("start_date"), cfg.get("end_date")

    # ---- 数据加载（含 ST 过滤）----
    universe = _filter_st(universe, cfg.get("exclude_st", True), data_dir)
    loader = datafeed.load_minute5 if period == "minute5" else datafeed.load_daily
    data = loader(universe, start, end, data_dir)
    if not data:
        raise RuntimeError(f"回测窗口内无数据（universe={universe}, {start}~{end}, {period}）")

    # ---- 信号 ----
    prepared = strategy.prepare(data, params)

    # risk_config 未显式设置止损而策略参数给了 stop_loss_pct → 覆盖
    risk_cfg_dict = dict(cfg.get("risk_config") or {})
    if "stop_loss_pct" in params and "stop_loss_pct" not in risk_cfg_dict:
        risk_cfg_dict["stop_loss_pct"] = params["stop_loss_pct"]
    risk_cfg = RiskConfig(risk_cfg_dict)

    # 为 ATR 止损模式预计算 ATR 列
    if risk_cfg.stop_loss_mode == "atr":
        atr_n = risk_cfg.atr_period
        prepared = {c: add_atr(df, atr_n, name=f"atr{atr_n}")
                    for c, df in prepared.items()}

    return _simulate(cfg, prepared, params, risk_cfg, data_dir, progress_cb)


# ------------------------------------------------------------------

def _filter_st(universe: list[str], exclude_st: bool, data_dir) -> list[str]:
    if not exclude_st:
        return universe
    basic = store.read_stock_basic(data_dir)
    if basic is None:
        return universe
    st_codes = set(basic.filter(pl.col("st")).select("code").to_series().to_list())
    filtered = [c for c in universe if c not in st_codes]
    return filtered or universe


def _stock_names(codes: list[str], data_dir) -> dict[str, str]:
    basic = store.read_stock_basic(data_dir)
    if basic is None:
        return {c: c for c in codes}
    m = dict(zip(basic["code"].to_list(), basic["name"].to_list()))
    return {c: m.get(c, c) for c in codes}


def _simulate(cfg: dict, prepared: dict[str, pl.DataFrame], params: dict,
              risk_cfg: RiskConfig, data_dir, progress_cb) -> dict:
    broker = Broker(cfg["slippage_pct"], cfg["commission_rate"], cfg["commission_min"],
                    cfg["stamp_tax"], cfg["transfer_fee"])
    risk_mgr = RiskManager(risk_cfg)
    portfolio = Portfolio(float(cfg["initial_capital"]))
    names = _stock_names(list(prepared.keys()), data_dir)

    # bars: code -> list[dict]；index: code -> {date: idx}
    bars, index = {}, {}
    for code, df in prepared.items():
        recs = df.to_dicts()
        bars[code] = recs
        index[code] = {r["date"]: i for i, r in enumerate(recs)}
    timeline = sorted({r["date"] for recs in bars.values() for r in recs})
    days = sorted({t[:10] for t in timeline})
    next_day = {days[i]: days[i + 1] for i in range(len(days) - 1)}

    # 前收盘（涨跌停判定：日线用前日收盘，分钟用前一交易日收盘）
    prev_daily_close: dict[str, dict[str, Optional[float]]] = {}
    for code, recs in bars.items():
        day_close: dict[str, float] = {}
        for r in recs:
            day_close[r["date"][:10]] = r["close"]  # 后复权
        code_days = sorted(day_close)
        prev_daily_close[code] = {code_days[i]: (day_close[code_days[i - 1]] if i else None)
                                  for i in range(len(code_days))}

    # ---- 运行状态 ----
    trade_log: list[dict] = []
    equity_curve: list[dict] = []
    snapshots: list[dict] = []
    price_map: dict[str, float] = {}
    pending: dict[str, dict] = {}      # code -> 下一bar执行的单
    t_state: dict[str, dict] = {}      # code -> 做T债务 {sold, bought, sell_amt, buy_amt}（跨日保留直至还清/清仓作废）
    adds_count: dict[str, int] = {}    # code -> 当前持仓期内加仓次数
    max_adds = int(params.get("max_adds") or 0)
    t_cycle_pnls: list[float] = []     # 已完成的做T周期盈亏（卖旧-买回价差，跨日持续至还清）
    state = {"intraday_trades": 0, "commission_total": 0.0, "trade_seq": 0}

    def _t_state(code: str) -> dict:
        return t_state.setdefault(code, {"sold": 0, "bought": 0,
                                         "sell_amt": 0.0, "buy_amt": 0.0})

    # ---- 闭包工具 ----

    def log_trade(code, bar, side, price, volume, fee, ttype, group_id, reason,
                  pnl=None, tag="", open_time=""):
        state["trade_seq"] += 1
        state["commission_total"] += fee
        factor = float(bar.get("adj_factor") or 1.0)
        raw_price = round(price / factor, 4)
        trade_log.append({
            "trade_id": state["trade_seq"], "code": code, "name": names.get(code, code),
            "time": bar["date"], "side": side,
            "price": raw_price, "hfq_price": round(price, 4),
            "volume": int(volume), "amount": round(raw_price * volume, 2),
            "fee": round(fee, 2), "type": ttype, "group_id": group_id,
            "reason": reason, "pnl": (round(pnl, 2) if pnl is not None else None),
            "tag": tag, "open_time": open_time,
        })

    def execute_sell(code, bar, volume_wanted, ttype, reason, exec_price):
        """FIFO 平仓；只卖 sellable（T+1）仓位；一字跌停不成交"""
        day = bar["date"][:10]
        pc = prev_daily_close[code].get(day)
        if broker.is_limit_down(bar, pc):
            return 0
        positions = sorted([p for p in portfolio.positions_of(code)
                            if p.sellable_date and day >= p.sellable_date],
                           key=lambda p: p.open_time)
        if not positions:
            return 0
        sellable_vol = sum(p.volume for p in positions)
        vol = sellable_vol if volume_wanted is None else min(volume_wanted, sellable_vol)
        vol = vol // 100 * 100
        if vol <= 0:
            return 0
        total_fee = broker.sell_fee(exec_price * vol)
        remaining = vol
        for pos in list(positions):
            if remaining <= 0:
                break
            take = min(pos.volume, remaining)
            remaining -= take
            fee_share = total_fee * take / vol
            pnl = (exec_price - pos.cost_price) * take - fee_share
            same_day = pos.open_time[:10] == day  # 当日买当日卖 → T交易
            log_ttype = "做T" if same_day else ttype
            log_trade(code, bar, "sell", exec_price, take, fee_share, log_ttype,
                      pos.group_id, reason, pnl=pnl, tag=pos.tag, open_time=pos.open_time)
            pos.volume -= take
            if pos.volume <= 0:
                portfolio.positions.remove(pos)
        portfolio.cash += exec_price * vol - total_fee
        state["intraday_trades"] += 1
        if not portfolio.positions_of(code):
            adds_count.pop(code, None)  # 清仓后重置加仓计数
            t_state.pop(code, None)     # 仓位清零：未还清的做T债务作废（该部分已按平仓盈亏入账）
        return vol

    def execute_buy(code, bar, order):
        """开仓/加仓/做T买回：本bar开盘价成交，一字涨停不成交，资金不足缩量"""
        day = bar["date"][:10]
        pc = prev_daily_close[code].get(day)
        if broker.is_limit_up(bar, pc):
            return
        if risk_mgr.broken:
            return
        if state["intraday_trades"] >= risk_cfg.max_intraday_trades:
            return
        price = broker.buy_price(bar["open"])
        equity = portfolio.equity(price_map)
        total_mv = portfolio.market_value(price_map)
        code_mv = sum(p.volume * price_map.get(code, p.cost_price)
                      for p in portfolio.positions_of(code))
        budget = risk_mgr.buy_budget(equity, total_mv, code, code_mv, portfolio.cash)
        tag = order.get("tag", "开仓")
        st = _t_state(code)
        vol = 0

        if tag == "做T":
            debt = (st["sold"] - st["bought"]) // 100 * 100
            if debt >= 100:
                # 买回做T卖出的筹码（债务跨日保留，直至还清）
                amount = min(debt * price, budget)
                vol = broker.lots_for_amount(amount, price)
            elif portfolio.volume_of(code) == 0:
                # 底仓已被止损/清仓：网格买点重建底仓
                tag = "开仓"
            else:
                return
        if tag in ("开仓", "加仓"):
            budget_pct = order.get("budget_pct")
            if budget_pct:
                budget = min(budget, equity * float(budget_pct) / 100)
            vol = broker.lots_for_amount(budget, price)
        if vol < 100:
            return
        # 资金不足按可用资金缩量（含费用余量）
        while vol >= 100 and vol * price + broker.buy_fee(vol * price) > portfolio.cash:
            vol -= 100
        if vol < 100:
            return
        amount = vol * price
        fee = broker.buy_fee(amount)
        portfolio.cash -= amount + fee
        state["intraday_trades"] += 1

        if tag == "做T":
            pos = portfolio.add_position(code, vol, price, bar["date"],
                                         next_day.get(day, "9999-12-31"), "做T", fee)
            st["bought"] += vol
            st["buy_amt"] += price * vol
            log_trade(code, bar, "buy", price, vol, fee, "做T", pos.group_id,
                      order.get("reason", "网格买回"))
            if st["bought"] >= st["sold"]:
                # 做T周期完成：卖旧与买回的价差即为做T贡献
                t_cycle_pnls.append(st["sell_amt"] - st["buy_amt"])
                t_state[code] = {"sold": 0, "bought": 0, "sell_amt": 0.0, "buy_amt": 0.0}
        elif tag == "加仓":
            same_code = portfolio.positions_of(code)
            base = same_code[-1] if same_code else None
            grp = base.group_id if base else portfolio.next_group_id()
            pos = portfolio.add_position(code, vol, price, bar["date"],
                                         next_day.get(day, "9999-12-31"), "加仓", fee,
                                         group_id=grp)
            adds_count[code] = adds_count.get(code, 0) + 1
            log_trade(code, bar, "buy", price, vol, fee, "加仓", grp,
                      order.get("reason", "加仓"))
        else:  # 开仓
            pos = portfolio.add_position(code, vol, price, bar["date"],
                                         next_day.get(day, "9999-12-31"), "开仓", fee)
            adds_count[code] = 0
            log_trade(code, bar, "buy", price, vol, fee, "开仓", pos.group_id,
                      order.get("reason", "买入信号"))

    def execute_order(order, code, bar):
        if order["side"] == "buy":
            execute_buy(code, bar, order)
        else:
            day = bar["date"][:10]
            if order.get("tag") == "做T":
                # 做T：卖出部分可卖底仓（约1/3）
                positions = [p for p in portfolio.positions_of(code)
                             if p.sellable_date and day >= p.sellable_date]
                sellable = sum(p.volume for p in positions)
                want = max(100, sellable // 3 // 100 * 100) if sellable >= 300 else 0
                price = broker.sell_price(bar["open"])
                sold = execute_sell(code, bar, want, "做T", order.get("reason", "网格卖出"), price)
                if sold:
                    st = _t_state(code)
                    st["sold"] += sold
                    st["sell_amt"] += price * sold
            else:  # 清仓信号
                execute_sell(code, bar, None, "清仓",
                             order.get("reason", "卖出信号"), broker.sell_price(bar["open"]))

    def check_stops(code, bar):
        """止损/止盈/移动止损：本bar收盘判定、收盘价成交（受跌停约束）"""
        day = bar["date"][:10]
        for pos in list(portfolio.positions_of(code)):
            if not (pos.sellable_date and day >= pos.sellable_date):
                continue  # T+1：当日买入不可卖
            pos.highest_price = max(pos.highest_price, bar["high"])
            atr = bar.get(f"atr{risk_cfg.atr_period}") or bar.get("d_atr") or bar.get("atr")
            hit = risk_mgr.check_stop(pos, bar["close"], atr)
            if hit:
                action, reason = hit
                ttype = "止损" if action == "stop_loss" else "止盈"
                execute_sell(code, bar, pos.volume, ttype, reason,
                             broker.sell_price(bar["close"]))

    def drawdown_now(equity: float) -> float:
        peak = max([p["equity"] for p in equity_curve], default=equity)
        peak = max(peak, equity)
        return equity / peak - 1 if peak > 0 else 0.0

    # ---------------- 主循环 ----------------
    n_bars = len(timeline)
    cur_day = None
    for ti, t in enumerate(timeline):
        day = t[:10]
        if day != cur_day:  # 新交易日：重置日内状态（做T债务 t_state 跨日保留直至还清）
            cur_day = day
            state["intraday_trades"] = 0

        for code in bars:
            i = index[code].get(t)
            if i is None:
                continue  # 停牌/无bar
            bar = bars[code][i]
            price_map[code] = bar["close"]

            # 1) 执行上一bar信号（本bar开盘价成交，避免未来函数）
            if code in pending:
                execute_order(pending.pop(code), code, bar)
            # 2) 风控：止损/止盈/移动止损（本bar收盘判定）
            check_stops(code, bar)
            # 3) 生成本bar信号 → 下一bar执行
            sig = bar.get("signal") or 0
            if sig == 1:
                tag = bar.get("tag") or "开仓"
                has_pos = portfolio.volume_of(code) > 0
                if tag in ("开仓", "加仓") and has_pos:
                    if adds_count.get(code, 0) >= max_adds:
                        continue  # 加仓次数用尽
                    tag = "加仓"
                pending[code] = {
                    "side": "buy",
                    "tag": tag if tag in ("开仓", "做T", "加仓") else "开仓",
                    "reason": bar.get("reason") or "买入信号",
                    # 开仓与做T信号均带 base_pct 预算：做T信号在无底仓时用于重建底仓
                    "budget_pct": params.get("base_pct") if tag in ("开仓", "做T") else None,
                }
            elif sig == -1:
                tag = bar.get("tag") or ""
                pending[code] = {"side": "sell",
                                 "tag": "做T" if tag == "做T" else "清仓",
                                 "reason": bar.get("reason") or "卖出信号"}

        # 日终：更新净值与资金曲线
        is_last_bar_of_day = ti + 1 >= n_bars or timeline[ti + 1][:10] != day
        if is_last_bar_of_day:
            equity = portfolio.equity(price_map)
            mv = portfolio.market_value(price_map)
            risk_mgr.update_equity(equity)
            equity_curve.append({"date": day, "equity": round(equity, 2),
                                 "drawdown": round(drawdown_now(equity), 6),
                                 "position_ratio": round(mv / equity, 4) if equity else 0.0})
            positions_snapshot = []
            for c, v in _code_volumes(portfolio).items():
                cost = _avg_cost_hfq(portfolio, c)
                factor = _factor_at(bars, c, day)
                positions_snapshot.append({"code": c, "volume": v,
                                           "cost": round(cost / factor, 4) if factor else round(cost, 4)})
            snapshots.append({"date": day, "cash": round(portfolio.cash, 2),
                              "market_value": round(mv, 2), "positions": positions_snapshot})
        if progress_cb and ti % 50 == 0:
            progress_cb(min(99.0, ti / n_bars * 100), f"回测中: {t}")

    # ---------------- 收尾 ----------------
    end_equity = portfolio.equity(price_map)
    metrics = build_metrics(trade_log, equity_curve, portfolio.initial_cash,
                            end_equity, state["commission_total"],
                            t_cycle_pnls=t_cycle_pnls)
    mret = monthly_returns(equity_curve, portfolio.initial_cash)
    if progress_cb:
        progress_cb(100, "回测完成")

    report = {
        "name": cfg.get("name", ""),
        "config": cfg,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "monthly_returns": mret,
        "trade_log": trade_log,
        "position_snapshots": snapshots,
    }
    if cfg.get("task_id"):
        report["task_id"] = cfg["task_id"]
    return report


def _code_volumes(portfolio: Portfolio) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in portfolio.positions:
        out[p.code] = out.get(p.code, 0) + p.volume
    return out


def _avg_cost_hfq(portfolio: Portfolio, code: str) -> float:
    vol = sum(p.volume for p in portfolio.positions if p.code == code)
    if vol == 0:
        return 0.0
    return sum(p.cost_price * p.volume for p in portfolio.positions if p.code == code) / vol


def _factor_at(bars: dict, code: str, day: str) -> float:
    for r in bars.get(code, []):
        if r["date"][:10] == day:
            return float(r.get("adj_factor") or 1.0)
    return 1.0
