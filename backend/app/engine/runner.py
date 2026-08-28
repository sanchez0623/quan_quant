# -*- coding: utf-8 -*-
"""回测主流程：向量化信号 -> 逐bar撮合（T+1、涨跌停、滑点、手续费、分层持仓、风控）
支持：指标预热期前推、按比例减仓、金字塔加仓预算、动态T比例、月度出金
（逐笔T盈利提成 + 月末兜底，统计基于"加回出金的调整净值"）。
输出契约 report 结构：metrics / equity_curve / monthly_returns / trade_log /
position_snapshots / withdrawal_log
"""
from datetime import datetime, timedelta
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
    "commission_rate": 0.00005,
    "commission_min": 5.0,
    "stamp_tax": 0.0005,
    "transfer_fee": 0.00001,
    "handling_fee": 0.0000341,
    "regulatory_fee": 0.00002,
    "exclude_st": True,
    "warmup_days": 0,
    # 撮合进阶：成交量参与率上限 + 市场冲击滑点（0 关闭，退化为固定滑点/不限量）
    "volume_participation": 0.1,
    "impact_k": 0.1,
    "monthly_withdraw_base": 0.0,
    "t_profit_withdraw_pct": 10.0,
    "min_t_amount": 20000.0,
    # 策略未传 t_ratio/reduce_pct 时的引擎兜底比例（%）：参数化，不再写死 1/3
    "t_ratio_fallback": 33.3333,
    "reduce_pct_fallback": 33.3333,
    # 止损成交口径：next_open = bar收盘判定、次bar开盘成交（诚实化，缺5分钟缺口）；
    # close = 旧口径，同bar收盘判定+同bar收盘成交（仅用于泄漏量对照）
    "stop_fill": "next_open",
}


def _shift_back(d: str, trading_days: int) -> str:
    """交易日数 -> 自然日近似（7/5）并留缓冲"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return (dt - timedelta(days=int(trading_days * 1.5) + 7)).strftime("%Y-%m-%d")


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

    # ---- 预热期：策略建议值与显式配置取较大者，前推数据加载窗口 ----
    warmup = int(cfg.get("warmup_days") or 0)
    if warmup <= 0:
        warmup = int(getattr(strategy, "warmup_days", 0) or 0)
    load_start = _shift_back(start, warmup) if warmup > 0 else start

    # ---- 数据加载（含 ST 过滤）----
    universe = _filter_st(universe, cfg.get("exclude_st", True), data_dir)
    loader = datafeed.load_minute5 if period == "minute5" else datafeed.load_daily
    data = loader(universe, load_start, end, data_dir)
    if not data:
        raise RuntimeError(f"回测窗口内无数据（universe={universe}, {start}~{end}, {period}）")

    # ---- 信号（start_date 之前为预热期，策略只算指标不推进状态机）----
    prepared = strategy.prepare(data, params, start_date=start)

    # risk_config 未显式设置止损而策略参数给了 stop_loss_pct -> 覆盖
    risk_cfg_dict = dict(cfg.get("risk_config") or {})
    if "stop_loss_pct" in params and "stop_loss_pct" not in risk_cfg_dict:
        risk_cfg_dict["stop_loss_pct"] = params["stop_loss_pct"]
    # 日内交易次数默认对齐策略 max_t_times（未显式配置时），避免引擎兜底值 4 与策略上限脱节；
    # max_t_times=0（关闭做T）时不对齐，保留风控默认值，避免误拦趋势交易
    if (not risk_cfg_dict.get("max_intraday_trades")
            and params.get("max_t_times", 0) and int(params["max_t_times"]) > 0):
        risk_cfg_dict["max_intraday_trades"] = int(params["max_t_times"])
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


def _st_map(codes: list[str], data_dir) -> dict[str, bool]:
    """code -> 是否 ST（用于差异化涨跌幅判定）；无 basic 时默认 False（主板10%）"""
    basic = store.read_stock_basic(data_dir)
    if basic is None:
        return {c: False for c in codes}
    st_codes = set(basic.filter(pl.col("st")).select("code").to_series().to_list())
    return {c: c in st_codes for c in codes}


def _simulate(cfg: dict, prepared: dict[str, pl.DataFrame], params: dict,
              risk_cfg: RiskConfig, data_dir, progress_cb) -> dict:
    broker = Broker(cfg["slippage_pct"], cfg["commission_rate"], cfg["commission_min"],
                    cfg["stamp_tax"], cfg["transfer_fee"],
                    cfg.get("handling_fee", 0.0), cfg.get("regulatory_fee", 0.0),
                    volume_participation=float(cfg.get("volume_participation", 0.0)),
                    impact_k=float(cfg.get("impact_k", 0.0)))
    risk_mgr = RiskManager(risk_cfg)
    portfolio = Portfolio(float(cfg["initial_capital"]))
    names = _stock_names(list(prepared.keys()), data_dir)
    st_map = _st_map(list(prepared.keys()), data_dir)

    def _lpct(code: str, day: str) -> float:
        return Broker.limit_pct(code, st_map.get(code, False), day)
    start_date = cfg.get("start_date") or ""
    # 兜底比例（策略未传 t_ratio/reduce_pct 时）：来自 DEFAULTS，可被配置覆盖
    t_ratio_fb = float(cfg.get("t_ratio_fallback") or 33.3333)
    reduce_fb = float(cfg.get("reduce_pct_fallback") or 33.3333)

    # ---- 出金配置 ----
    wd_base = float(cfg.get("monthly_withdraw_base") or 0)
    wd_pct = float(cfg.get("t_profit_withdraw_pct") or 0)
    min_t_amount = float(cfg.get("min_t_amount") or 0)
    w_state = {"total": 0.0, "t_profit": 0.0, "topup": 0.0,
               "shortfall": 0.0, "recover": 0.0, "months": {}, "log": []}

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
    pending_stops: dict[str, list] = {}  # code -> 待执行的止损/止盈单（next_open，一字跌停顺延）
    stop_fill = str(cfg.get("stop_fill") or "next_open")
    t_state: dict[str, dict] = {}      # code -> 做T债务 {sold, bought, sell_amt, buy_amt}（跨日保留直至还清/清仓作废）
    adds_count: dict[str, int] = {}    # code -> 当前持仓期内加仓次数
    max_adds = int(params.get("max_adds") or 0)
    t_cycle_pnls: list[float] = []     # 已完成的做T周期盈亏（卖旧-买回价差，跨日持续至还清）
    state = {"intraday_trades": {}, "commission_total": 0.0, "trade_seq": 0}  # code -> 当日交易次数

    def _t_state(code: str) -> dict:
        return t_state.setdefault(code, {"sold": 0, "bought": 0,
                                         "sell_amt": 0.0, "buy_amt": 0.0})

    def _trades_today(code: str) -> int:
        return state["intraday_trades"].get(code, 0)

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

    def execute_sell(code, bar, volume_wanted, ttype, reason, basis_price):
        """FIFO 平仓；只卖 sellable（T+1）仓位；一字跌停不成交；卖出允许零股。
        basis_price 为原始成交价基准（open/close），滑点与成交量约束在此统一计算。"""
        day = bar["date"][:10]
        pc = prev_daily_close[code].get(day)
        if broker.is_limit_down(bar, pc, _lpct(code, day)):
            return 0, 0.0
        positions = sorted([p for p in portfolio.positions_of(code)
                            if p.sellable_date and day >= p.sellable_date],
                           key=lambda p: p.open_time)
        if not positions:
            return 0, 0.0
        sellable_vol = sum(p.volume for p in positions)
        vol = sellable_vol if volume_wanted is None else min(volume_wanted, sellable_vol)
        if vol <= 0:
            return 0, 0.0
        # 成交量参与率约束：单笔不得超过当 bar 成交量的一定比例（流动性不足时缩量）
        bar_vol = float(bar.get("volume") or 0)
        vol = broker.cap_volume(int(vol), bar_vol)
        if vol <= 0:
            return 0, 0.0
        # 市场冲击滑点：订单占当 bar 成交量比例越大，滑点越高
        exec_price = broker.sell_price(basis_price, bar_vol, vol)
        # 金额一律按真实市场价（raw）计：引擎内部价是后复权价，需除以复权因子
        factor = float(bar.get("adj_factor") or 1.0)
        raw_price = exec_price / factor
        total_fee = broker.sell_fee(raw_price * vol)
        remaining = vol
        for pos in list(positions):
            if remaining <= 0:
                break
            take = min(pos.volume, remaining)
            remaining -= take
            fee_share = total_fee * take / vol
            # 平仓盈亏：后复权价差折算回真实价（/factor）再减真实手续费
            pnl = (exec_price - pos.cost_price) * take / factor - fee_share
            same_day = pos.open_time[:10] == day  # 当日买当日卖 -> T交易
            log_ttype = "做T" if same_day else ttype
            log_trade(code, bar, "sell", exec_price, take, fee_share, log_ttype,
                      pos.group_id, reason, pnl=pnl, tag=pos.tag, open_time=pos.open_time)
            pos.volume -= take
            if pos.volume <= 0:
                portfolio.positions.remove(pos)
        portfolio.cash += raw_price * vol - total_fee
        state["intraday_trades"][code] = _trades_today(code) + 1
        if not portfolio.positions_of(code):
            adds_count.pop(code, None)  # 清仓后重置加仓计数
            t_state.pop(code, None)     # 仓位清零：未还清的做T债务作废（该部分已按平仓盈亏入账）
        return vol, exec_price

    def execute_buy(code, bar, order):
        """开仓/加仓/做T买回：本bar开盘价成交，一字涨停不成交，资金不足缩量。
        成交量参与率约束 + 市场冲击滑点。"""
        day = bar["date"][:10]
        pc = prev_daily_close[code].get(day)
        if broker.is_limit_up(bar, pc, _lpct(code, day)):
            return
        if risk_mgr.broken:
            # 回撤熔断期间禁止买入；回撤已修复且是策略主动建仓信号（开仓/加仓）→ 解除熔断恢复交易
            if not risk_mgr.try_resume(order.get("tag") in ("开仓", "加仓")):
                return
        if _trades_today(code) >= risk_cfg.max_intraday_trades:
            return
        bar_vol = float(bar.get("volume") or 0)
        # 先用基础滑点价（不含冲击）做仓位测算，确定量后再算含冲击的真实成交价
        price = broker.buy_price(bar["open"])
        # 金额/预算一律按真实市场价（raw）计：引擎内部价是后复权价，需除以复权因子
        factor = float(bar.get("adj_factor") or 1.0)
        raw_price = price / factor
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
                amount = min(debt * raw_price, budget)
                vol = broker.lots_for_amount(amount, raw_price)
            elif portfolio.volume_of(code) == 0:
                # 底仓已被止损/清仓：网格买点重建底仓
                tag = "开仓"
            else:
                return
        if tag in ("开仓", "加仓"):
            if tag == "开仓" and risk_cfg.max_holdings > 0:
                held = {p.code for p in portfolio.positions}
                if code not in held and len(held) >= risk_cfg.max_holdings:
                    return  # 持仓只数已达上限（只限制新开仓，不影响持有/加仓/做T）
            budget_pct = order.get("budget_pct")
            if budget_pct:
                budget = min(budget, equity * float(budget_pct) / 100)
            vol = broker.lots_for_amount(budget, raw_price)
        if vol < 100:
            return
        # 成交量参与率约束：流动性不足时缩量（整百）
        vol = broker.cap_volume(int(vol), bar_vol)
        if vol < 100:
            return
        # 含市场冲击的真实成交价（订单占比越大滑点越高）
        price = broker.buy_price(bar["open"], bar_vol, vol)
        raw_price = price / factor
        # 资金不足按可用资金缩量（含费用余量）
        while vol >= 100 and vol * raw_price + broker.buy_fee(vol * raw_price) > portfolio.cash:
            vol -= 100
        if vol < 100:
            return
        amount = vol * raw_price
        fee = broker.buy_fee(amount)
        portfolio.cash -= amount + fee
        state["intraday_trades"][code] = _trades_today(code) + 1

        if tag == "做T":
            pos = portfolio.add_position(code, vol, price, bar["date"],
                                         next_day.get(day, "9999-12-31"), "做T", fee)
            st["bought"] += vol
            st["buy_amt"] += raw_price * vol
            log_trade(code, bar, "buy", price, vol, fee, "做T", pos.group_id,
                      order.get("reason", "网格买回"))
            if st["bought"] >= st["sold"]:
                # 做T周期完成：卖旧与买回的价差即为做T贡献
                pnl = st["sell_amt"] - st["buy_amt"]
                t_cycle_pnls.append(pnl)
                t_state[code] = {"sold": 0, "bought": 0, "sell_amt": 0.0, "buy_amt": 0.0}
                # 逐笔出金：该笔T盈利即时提取 x%（落袋为安）
                if pnl > 0 and wd_pct > 0:
                    amt = min(pnl * wd_pct / 100.0, portfolio.cash)
                    if amt > 0:
                        portfolio.cash -= amt
                        w_state["total"] += amt
                        w_state["t_profit"] += amt
                        month = day[:7]
                        w_state["months"][month] = w_state["months"].get(month, 0.0) + amt
                        w_state["log"].append({"month": month, "date": bar["date"],
                                               "type": "t_profit", "amount": round(amt, 2)})
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
                # 做T：按动态比例卖出可卖底仓（整百，与买回债务对齐）
                positions = [p for p in portfolio.positions_of(code)
                             if p.sellable_date and day >= p.sellable_date]
                sellable = sum(p.volume for p in positions)
                ratio = float(order.get("t_ratio") or t_ratio_fb) / 100.0
                want = int(sellable * ratio) // 100 * 100
                # 最小T金额保护：用基础滑点价估算（实际成交价含冲击，差异很小），按真实市场价计
                est_price = broker.sell_price(bar["open"])
                raw_est = est_price / float(bar.get("adj_factor") or 1.0)
                if want < 100 or (min_t_amount > 0 and want * raw_est < min_t_amount):
                    return
                sold, fill_price = execute_sell(code, bar, want, "做T",
                                                order.get("reason", "网格卖出"), bar["open"])
                if sold:
                    st = _t_state(code)
                    st["sold"] += sold
                    st["sell_amt"] += (fill_price / float(bar.get("adj_factor") or 1.0)) * sold
            elif order.get("tag") == "减仓":
                # 按比例减仓：过热锁盈 / 分批止盈。
                # A股卖出申报同样为100股整数倍（不足100股的零股只能一次性清仓卖出），
                # 向下取整到整百，避免减仓后留下非整百零股持仓。
                positions = [p for p in portfolio.positions_of(code)
                             if p.sellable_date and day >= p.sellable_date]
                sellable = sum(p.volume for p in positions)
                reduce_pct = float(order.get("reduce_pct") or reduce_fb)
                want = int(sellable * reduce_pct / 100.0) // 100 * 100
                if want > 0:
                    execute_sell(code, bar, want, "减仓",
                                 order.get("reason", "减仓"), bar["open"])
            else:  # 清仓信号
                execute_sell(code, bar, None, "清仓",
                             order.get("reason", "卖出信号"), bar["open"])

    def check_stops(code, bar):
        """止损/止盈/移动止损：本bar收盘判定。
        next_open 口径：判定后写入 pending_stops，次bar开盘成交（一字跌停顺延）；
        close 口径（旧）：同bar收盘判定、同bar收盘成交，仅用于泄漏量对照。"""
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
                if stop_fill == "next_open":
                    pending_stops.setdefault(code, []).append(
                        {"code": code, "pos": pos, "volume": pos.volume,
                         "ttype": ttype, "reason": reason})
                else:
                    execute_sell(code, bar, pos.volume, ttype, reason, bar["close"])

    def execute_pending_stops(code, bar):
        """执行上一bar挂起的止损单（本bar开盘成交，优先级高于策略信号）。
        一字跌停无法成交 -> 顺延到下一bar；仓位已被其它单清掉 -> 作废。"""
        queued = pending_stops.get(code)
        if not queued:
            return
        kept = []
        for o in queued:
            if not any(o["pos"] is p for p in portfolio.positions):
                continue  # 该仓位已平（策略/其它止损），作废
            sold, _ = execute_sell(code, bar, o["volume"], o["ttype"], o["reason"],
                                   bar["open"])
            if sold <= 0:
                kept.append(o)  # 一字跌停未成交 -> 顺延
        if kept:
            pending_stops[code] = kept
        else:
            pending_stops.pop(code, None)

    def drawdown_now(adj_equity: float) -> float:
        peak = max([p["adjusted_equity"] for p in equity_curve], default=adj_equity)
        peak = max(peak, adj_equity)
        return adj_equity / peak - 1 if peak > 0 else 0.0

    def month_settle(day: str) -> None:
        """月末出金兜底：当月累计提取不足目标额则补齐（不吃本金、现金不足记缺口）；
        满足当月目标后，若有剩余利润空间且存在历史 shortfall，优先追偿"""
        month = day[:7]
        month_total = w_state["months"].get(month, 0.0)
        if wd_base <= 0:
            return
        # 1) 先处理当月目标
        need = wd_base - month_total
        if need > 0:
            equity = portfolio.equity(price_map)
            # 护栏：累计提取不得超过累计盈利水位（本金永不被提取）
            profit_room = max(0.0, equity + w_state["total"] - portfolio.initial_cash)
            allowed = profit_room - w_state["total"]
            amt = max(0.0, min(need, portfolio.cash, allowed))
            if amt > 0:
                portfolio.cash -= amt
                w_state["total"] += amt
                w_state["topup"] += amt
                w_state["months"][month] = month_total + amt
                w_state["log"].append({"month": month, "date": day,
                                       "type": "month_topup", "amount": round(amt, 2)})
                need -= amt
            if need > 0:  # 现金不足或护栏封顶：缺口挂账记录
                w_state["shortfall"] += need
                w_state["log"].append({"month": month, "date": day,
                                       "type": "shortfall", "amount": round(need, 2)})
        # 2) 当月目标已满足（month_total >= wd_base），若有剩余利润空间且存在历史 shortfall，优先追偿
        current_month_total = w_state["months"].get(month, 0.0)
        if current_month_total >= wd_base and w_state["shortfall"] > 0:
            equity = portfolio.equity(price_map)
            profit_room = max(0.0, equity + w_state["total"] - portfolio.initial_cash)
            allowed = profit_room - w_state["total"]  # 剩余可提取利润空间
            if allowed > 0:
                # 追偿额 = min(剩余利润空间, 历史缺口)
                recover = min(allowed, w_state["shortfall"])
                if recover > 0:
                    # 追偿也需要卖出生成现金，受限于当前现金和 allowed
                    amt = max(0.0, min(recover, portfolio.cash, allowed))
                    if amt > 0:
                        portfolio.cash -= amt
                        w_state["total"] += amt
                        w_state["recover"] += amt  # 追偿单独记账：不属于当月月末补齐
                        w_state["shortfall"] -= amt
                        w_state["log"].append({"month": month, "date": day,
                                               "type": "shortfall_recover", "amount": round(amt, 2)})

    # ---------------- 主循环 ----------------
    n_bars = len(timeline)
    cur_day = None
    for ti, t in enumerate(timeline):
        day = t[:10]
        if day != cur_day:  # 新交易日：重置日内状态（做T债务 t_state 跨日保留直至还清）
            cur_day = day
            state["intraday_trades"] = {}

        in_warmup = bool(start_date) and day < start_date  # 预热期：只喂指标不交易
        for code in bars:
            i = index[code].get(t)
            if i is None:
                continue  # 停牌/无bar
            bar = bars[code][i]
            # 金额口径用真实市场价（后复权价除以复权因子）；价格比较（止损/ATR）仍用 bar 后复权价
            price_map[code] = bar["close"] / float(bar.get("adj_factor") or 1.0)
            if in_warmup:
                continue

            # 0) 执行上一bar挂起的止损（本bar开盘成交；一字跌停顺延至下一bar）
            execute_pending_stops(code, bar)
            # 1) 执行上一bar信号（本bar开盘价成交，避免未来函数）
            if code in pending:
                execute_order(pending.pop(code), code, bar)
            # 2) 风控：止损/止盈/移动止损（本bar收盘判定 -> next_open 挂起次bar成交）
            check_stops(code, bar)
            # 3) 生成本bar信号 -> 下一bar执行
            sig = bar.get("signal") or 0
            if sig == 1:
                tag = bar.get("tag") or "开仓"
                has_pos = portfolio.volume_of(code) > 0
                if tag in ("开仓", "加仓") and has_pos:
                    if adds_count.get(code, 0) >= max_adds:
                        continue  # 加仓次数用尽
                    tag = "加仓"
                budget_pct = bar.get("budget_pct")
                if tag in ("开仓", "做T") and budget_pct is None:
                    budget_pct = params.get("base_pct")
                pending[code] = {
                    "side": "buy",
                    "tag": tag if tag in ("开仓", "做T", "加仓") else "开仓",
                    "reason": bar.get("reason") or "买入信号",
                    "budget_pct": budget_pct,
                    "t_ratio": bar.get("t_ratio"),
                    "reduce_pct": bar.get("reduce_pct"),
                }
            elif sig == -1:
                tag = bar.get("tag") or ""
                pending[code] = {"side": "sell",
                                 "tag": tag if tag in ("做T", "减仓") else "清仓",
                                 "reason": bar.get("reason") or "卖出信号",
                                 "t_ratio": bar.get("t_ratio"),
                                 "reduce_pct": bar.get("reduce_pct")}

        # 日终：更新净值与资金曲线（调整净值 = 真实净值 + 累计提取，统计口径基准）
        is_last_bar_of_day = ti + 1 >= n_bars or timeline[ti + 1][:10] != day
        if is_last_bar_of_day and not in_warmup:
            equity = portfolio.equity(price_map)
            mv = portfolio.market_value(price_map)
            adj_equity = equity + w_state["total"]
            risk_mgr.update_equity(adj_equity)  # 熔断基于调整净值（出金不算亏损）
            equity_curve.append({"date": day, "equity": round(equity, 2),
                                 "adjusted_equity": round(adj_equity, 2),
                                 "drawdown": round(drawdown_now(adj_equity), 6),
                                 "position_ratio": round(mv / equity, 4) if equity else 0.0})
            positions_snapshot = []
            for c, v in _code_volumes(portfolio).items():
                cost = _avg_cost_hfq(portfolio, c)
                factor = _factor_at(bars, c, day)
                positions_snapshot.append({"code": c, "name": names.get(c, c), "volume": v,
                                           "cost": round(cost / factor, 4) if factor else round(cost, 4)})
            snapshots.append({"date": day, "cash": round(portfolio.cash, 2),
                              "market_value": round(mv, 2), "positions": positions_snapshot})
            # 月末判定：次日跨月或已是最后交易日 -> 出金兜底结算
            nd = next_day.get(day)
            if nd is None or nd[:7] != day[:7]:
                month_settle(day)
        if progress_cb and ti % 50 == 0:
            progress_cb(min(99.0, ti / n_bars * 100), f"回测中: {t}")

    # ---------------- 收尾 ----------------
    end_equity = portfolio.equity(price_map)
    withdrawn_summary = {
        "monthly_base": round(wd_base, 2),
        "total": round(w_state["total"], 2),
        "t_profit": round(w_state["t_profit"], 2),
        "month_topup": round(w_state["topup"], 2),
        "shortfall": round(w_state["shortfall"], 2),
        "recover": round(w_state["recover"], 2),
        "months": {m: round(v, 2) for m, v in w_state["months"].items()},
        "log": w_state["log"],
    }
    # coverage 分母：已过完的完整月份数 = equity_curve 覆盖的月份集合
    # （每个覆盖月份都在月末/末日各结算一次，等价于实际执行的月度结算次数，而非 len(months) 出金月数）
    completed_months = len({ec["date"][:7] for ec in equity_curve}) if equity_curve else 0
    metrics = build_metrics(trade_log, equity_curve, portfolio.initial_cash,
                            end_equity, state["commission_total"],
                            t_cycle_pnls=t_cycle_pnls, withdrawn=w_state,
                            wd_base=wd_base, completed_months=completed_months)
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
        "withdrawal": withdrawn_summary,
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
