# -*- coding: utf-8 -*-
"""龙头低吸二期：分钟级事件模拟（打板 / 分时低吸 / 竞价收复 / 首阴次日）。

为什么不是 runner.period="minute5"：全市场分钟线回测 = 5221 票 × 3.2 万 bar
（内存 ~30GB、单跑小时级）；且触板票 5157 只几乎等于全市场、前 400 票仅覆盖
18.7% 触板日——任何静态池都会漏掉绝大多数候选。故采用两阶段流水线：
- Stage 1 日线级上下文（全市场，复用 limitup_core）：涨停基因 / 市场情绪门控 /
  连板高度排名 / 每日候选资格 / MA5 与量能基准（~30s）
- Stage 2 分钟级事件模拟（只对候选票+持仓票流式加载分钟线，复用 Broker）：
  四买点 + 止损 / 破MA5 / 金字塔减仓 / 爆量滞涨 全部盘中化

买点语义（全部只依赖当日及更早数据，无未来函数）：
- dban 打板：bar.high 触及涨停价即按涨停价成交（dban_fill=touch 乐观 /
  break 保守=仅炸板 bar 成交；一字板 bar 不成交）
- dip 分时低吸：日内曾触板，现价自日内高点回落 [dip_pb_min, dip_pb_max]%
  -> 次根 bar 开盘买（日线版拿不到的盘中分时低点）
- gap 竞价收复：昨收涨停 + 今低开 >= gap_down_min% + 盘中收复当日开盘价
  -> 次根 bar 开盘买（日线版只能尾盘确认，分钟版在收复瞬间介入）
- yin 首阴次日：日线收盘确认首阴 -> 次日首根 bar 开盘买（与日线版同语义）
- entry_cutoff 后不新开仓（避免尾盘接刀），持仓退出不受限

退出（全部分钟级）：固定止损 / 破日线MA5 / 爆量滞涨（日终判定次日首根卖）/
涨停晋级金字塔减仓，均为收盘判定 -> 次根开盘成交；一字跌停 bar 卖不出顺延。
T+1 按日拦截（当日买入次日才可卖）。

价格口径：Stage 2 全程后复权价（datafeed.load_minute5 单票带缓存），
涨停判定用「原始涨停价 × 当日 adj_factor」，金额一律换算原始价。
分红除权日 raw_cost 不回溯调整（pnl 含分红效应，与 runner 口径略有差异）。

报告结构与 runner 兼容（metrics/equity_curve/trade_log/monthly_returns/
gate_days——gate_days 为策略情绪门控真值，勿与引擎级池/指数门控混淆）。
"""
import math
import statistics
from dataclasses import dataclass
from typing import Optional

import polars as pl

from .. import config
from ..data import store
from . import limitup_core as lc
from .broker import Broker
from .datafeed import load_minute5
from .stats import monthly_returns

_LIMIT_TOL = 0.011
_CTX_START = "2022-01-01"      # Stage 1 预热起点（新股 mature 计数基准）
_WINDOW_START = "2024-01-02"   # 分钟级回测最早窗口（minute5 覆盖范围）

DEFAULT_PARAMS: dict = {
    "entry_type": "all",        # all|dban|dip|gap|yin
    "top_n": 2,                 # 每日最多新开仓数
    "base_pct": 50.0,           # 单票预算占净值 %
    "exclude_one_word": "on",
    "regime_gate_on": "on",
    "board_window": 5,
    "min_boards": 3,
    "min_amount": 3.0,          # 亿
    "new_stock_days": 6,
    "dban_fill": "break",       # break|touch
    "dip_pb_min": 3.0,
    "dip_pb_max": 7.0,
    "entry_cutoff": "14:30",
    "gap_down_min": 5.0,
    "yin_min": 3.0,
    "yin_max": 5.0,
    "pyr_step": 30.0,
    "pyr_max": 3,
    "vol_burst_max": 3.0,
    "stall_gain_max": 2.0,
    "broken_rate_th": 0.40,
    "n_limit_floor": 20,
    "regime_ma_n": 10,
    "euphoria_boards": 6,
    "euphoria_scale": 0.6,
    "stop_loss_pct": 5.0,
    "max_holdings": 2,
    "max_position_pct_per_stock": 60.0,
    "max_drawdown_breaker": 30.0,
}

# code_day 行元组字段（code_day_row 返回 r[1:]，共 13 项）
_ROW_COLS = ["date", "prev_close", "limit_price", "is_limit_close", "touched_limit",
             "one_word", "consec_prev", "prev_amount", "close", "close_hfq",
             "ma5_hfq", "volume", "vol_ma20", "adj_factor"]
_IDX_PREV_CLOSE, _IDX_LIMIT, _IDX_VOL, _IDX_VOL_MA20, _IDX_FACTOR = 0, 1, 10, 11, 12
_IDX_CLOSE_HFQ = 9


# ---------------------------------------------------------------- Stage 1

@dataclass
class DailyContext:
    days: list[str]                                # 可回测交易日（升序）
    gate: dict[str, tuple[bool, bool]]             # day -> (gate_off, euphoria)
    frame: pl.DataFrame                            # 日线全量上下文
    candidates: dict[str, list[tuple]]             # day -> [(code, kinds)]
    yin_confirms: dict[str, list[str]]             # 确认日 -> 次日可买 code
    code_day: dict[str, dict[str, tuple]]          # code -> day -> 行元组（懒加载）
    next_day: dict[str, str]


def _build_daily_frame(codes: list[str], p: dict) -> pl.DataFrame:
    """全市场日线上下文（原始价涨停判定 + hfq 均线），自 _CTX_START 预热。"""
    basic = store.read_stock_basic()
    st_map = {r[0]: bool(r[1]) for r in basic.select(["code", "st"]).iter_rows()}
    daily = pl.read_parquet(config.DATA_DIR / "daily.parquet")
    daily = daily.filter(pl.col("code").is_in(codes) & (pl.col("date") >= _CTX_START))
    adj = store.read_adj_factor(codes)
    adj = adj.select([pl.col("code"),
                      pl.col("date").str.slice(0, 10).alias("date"),
                      pl.col("adj_factor").cast(pl.Float64)]).unique(
        subset=["code", "date"], keep="last")
    adj_by_code = adj.partition_by("code", as_dict=True)
    min_amt = float(p["min_amount"]) * 1e8
    bw = int(p["board_window"])
    keep = ["code", "date", "prev_close", "limit_price", "is_limit_close",
            "touched_limit", "limit_broken", "one_word", "consec_boards", "open",
            "close", "volume", "amount", "is_lc_prev", "one_word_prev",
            "consec_prev", "prev_amount", "close_hfq", "ma5_hfq", "vol_ma20",
            "adj_factor", "gene", "liquid", "mature"]
    parts: list[pl.DataFrame] = []
    for key, df in daily.group_by("code"):
        code = key[0] if isinstance(key, tuple) else key
        f = lc.daily_flags(df.sort("date").select(
            ["date", "open", "high", "low", "close", "volume", "amount"]),
            code, st_map.get(code, False))
        f = f.with_columns(pl.lit(code).alias("code"))
        f = (f.drop("adj_factor") if "adj_factor" in f.columns else f)
        adj_sub = adj_by_code.get(code if code in adj_by_code else (code,))
        if adj_sub is not None:
            f = f.join(adj_sub.select(["date", "adj_factor"]), on="date",
                       how="left")
        f = f.with_columns([
            pl.col("adj_factor").fill_null(1.0),
            (pl.col("close") * pl.col("adj_factor")).alias("close_hfq"),
            pl.col("is_limit_close").shift(1).fill_null(False).alias("is_lc_prev"),
            pl.col("one_word").shift(1).fill_null(True).alias("one_word_prev"),
            pl.col("consec_boards").shift(1).fill_null(0).alias("consec_prev"),
            pl.col("amount").shift(1).fill_null(0.0).alias("prev_amount"),
            pl.int_range(pl.len()).cast(pl.Int32).alias("_bar_no"),
            pl.col("volume").cast(pl.Float64).rolling_mean(20, min_samples=5)
            .alias("vol_ma20"),
            (pl.col("close") * pl.col("adj_factor")).rolling_mean(5, min_samples=5)
            .alias("ma5_hfq"),
        ])
        f = f.with_columns([
            (pl.col("touched_limit").cast(pl.Int32).rolling_sum(bw, min_samples=1)
             >= 1).alias("gene"),
            (pl.col("amount") >= min_amt).alias("liquid"),
            (pl.col("_bar_no") >= int(p["new_stock_days"])).alias("mature"),
        ])
        parts.append(f.select(keep))
    return pl.concat(parts)


def build_daily_context(codes: list[str], p: dict) -> DailyContext:
    """Stage 1：情绪门控 + 每日候选（排名截断）+ 首阴确认表。"""
    frame = _build_daily_frame(codes, p)
    sent = lc.market_sentiment([
        f for _, f in frame.group_by("code", maintain_order=False)
        if f.height >= 2])
    gate_df = (lc.regime_gate(sent, float(p["broken_rate_th"]),
                              int(p["n_limit_floor"]), int(p["regime_ma_n"]),
                              int(p["euphoria_boards"]))
               if str(p.get("regime_gate_on") or "on") == "on"
               else sent.select("date").with_columns(
                   [pl.lit(False).alias("gate_off"),
                    pl.lit(False).alias("euphoria")]))
    gate = {r[0]: (bool(r[1]), bool(r[2])) for r in gate_df.iter_rows()}
    frame = frame.with_columns(pl.Series(
        "gate_off", [gate.get(d, (False, False))[0] for d in frame["date"].to_list()]))

    et = str(p.get("entry_type") or "all")
    on = ({"dban", "dip", "gap", "yin"} if et == "all" else {et})
    fresh = ((~pl.col("one_word_prev"))
             if str(p.get("exclude_one_word") or "on") == "on" else pl.lit(True))
    base = pl.col("gene") & pl.col("liquid") & pl.col("mature") & ~pl.col("gate_off")
    pct_chg = pl.col("close") / pl.col("close").shift(1).over("code") - 1
    vol_ratio = pl.col("volume") / pl.col("vol_ma20")

    intraday_elig = ((base & pl.col("touched_limit") & ~pl.col("one_word"))
                     if ("dban" in on or "dip" in on) else pl.lit(False))
    gap_elig = (base & pl.col("is_lc_prev") & fresh
                if "gap" in on else pl.lit(False))
    yin_elig = ((pl.col("consec_prev") >= int(p["min_boards"]))
                & (pl.col("close") < pl.col("open"))
                & pct_chg.ge(-float(p["yin_max"]) / 100)
                & pct_chg.le(-float(p["yin_min"]) / 100)
                & (pl.col("close_hfq") >= pl.col("ma5_hfq"))
                & (vol_ratio >= 1.0)
                & (vol_ratio <= float(p["vol_burst_max"]))
                & base & fresh if "yin" in on else pl.lit(False))
    frame = frame.with_columns([
        intraday_elig.fill_null(False).alias("_touch_elig"),
        gap_elig.fill_null(False).alias("_gap_elig"),
        yin_elig.fill_null(False).alias("_yin_ok"),
    ])

    buf = max(6, int(p["top_n"]) * 4)
    cand = (frame.filter(pl.col("_touch_elig") | pl.col("_gap_elig"))
            .sort(["date", "consec_prev", "prev_amount", "code"],
                  descending=[False, True, True, False])
            .group_by("date", maintain_order=True).head(buf))
    candidates: dict[str, list[tuple]] = {}
    for day, code, te, ge in cand.select(["date", "code", "_touch_elig",
                                          "_gap_elig"]).iter_rows():
        kinds = set()
        if te and ("dban" in on or "dip" in on):
            kinds |= {"dban", "dip"}
        if ge and "gap" in on:
            kinds.add("gap")
        if kinds:
            candidates.setdefault(day, []).append((code, frozenset(kinds)))

    yin_confirms: dict[str, list[str]] = {}
    if "yin" in on:
        for day, code in frame.filter(pl.col("_yin_ok")).select(
                ["date", "code"]).iter_rows():
            yin_confirms.setdefault(day, []).append(code)

    all_days = sorted(frame["date"].unique().to_list())
    days = [d for d in all_days if d >= _WINDOW_START]
    next_day = {all_days[i]: all_days[i + 1] for i in range(len(all_days) - 1)}
    return DailyContext(days=days, gate=gate, frame=frame,
                        candidates=candidates, yin_confirms=yin_confirms,
                        code_day={}, next_day=next_day)


def code_day_row(ctx: DailyContext, code: str, day: str) -> Optional[tuple]:
    """懒加载 per-code 日线行元组（仅候选/持仓票触达）。"""
    m = ctx.code_day.get(code)
    if m is None:
        df = ctx.frame.filter(pl.col("code") == code)
        m = {r[0]: r[1:] for r in df.select(_ROW_COLS).iter_rows()}
        ctx.code_day[code] = m
    return m.get(day)


# ---------------------------------------------------------------- Stage 2

@dataclass
class _Pos:
    code: str
    vol: int
    hfq_cost: float
    raw_cost: float
    open_fee: float
    vol0: int
    open_time: str
    sellable: str
    first_hfq: float
    reductions: int = 0
    group_id: int = 0


@dataclass
class _Pending:
    side: str                     # buy|sell
    kind: str = ""
    reason: str = ""
    tag: str = ""
    budget: float = 0.0
    reduce_pct: Optional[float] = None


def run_minute_backtest(cfg: dict) -> dict:
    """二期分钟级回测入口。cfg：start_date/end_date/initial_capital/params/
    risk_config（stop_loss_pct / max_holdings / max_position_pct_per_stock /
    max_drawdown_breaker 以 risk_config 优先）。"""
    p = dict(DEFAULT_PARAMS)
    p.update({k: v for k, v in (cfg.get("params") or {}).items() if v is not None})
    risk = cfg.get("risk_config") or {}
    stop_pct = float(risk.get("stop_loss_pct", p["stop_loss_pct"]))
    max_hold = int(risk.get("max_holdings", p["max_holdings"]))
    max_pos_pct = float(risk.get("max_position_pct_per_stock",
                                 p["max_position_pct_per_stock"]))
    dd_breaker = float(risk.get("max_drawdown_breaker", p["max_drawdown_breaker"]))

    basic = store.read_stock_basic()
    st_map = {r[0]: bool(r[1]) for r in basic.select(["code", "st"]).iter_rows()}
    name_map = dict(zip(basic["code"].to_list(), basic["name"].to_list()))
    universe = basic.filter(~pl.col("delisted") & ~pl.col("st"))["code"].to_list()

    ctx = build_daily_context(universe, p)
    days = [d for d in ctx.days
            if (not cfg.get("start_date") or d >= cfg["start_date"])
            and (not cfg.get("end_date") or d <= cfg["end_date"])]
    if not days:
        raise RuntimeError("分钟级回测窗口内无交易日")

    broker = Broker(slippage_pct=0.001, commission_rate=0.00005,
                    commission_min=5.0, stamp_tax=0.0005, transfer_fee=0.00001,
                    handling_fee=0.0000341, regulatory_fee=0.00002,
                    volume_participation=0.1, impact_k=0.1)
    cash = float(cfg.get("initial_capital") or 1_000_000.0)
    initial = cash
    holdings: dict[str, _Pos] = {}
    pending: dict[str, _Pending] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    gate_days: dict[str, bool] = {}
    seq = {"n": 0}
    peak = {"v": initial}
    halted = {"on": False, "trough": initial, "stable": False}
    m5_cache: dict[str, Optional[dict[str, list[dict]]]] = {}
    day_state: dict[tuple, dict] = {}
    prev_day_map = {b: a for a, b in ctx.next_day.items()}

    def m5_bars(code: str) -> Optional[dict[str, list[dict]]]:
        if code in m5_cache:
            return m5_cache[code]
        df = load_minute5([code], _WINDOW_START, None, None).get(code)
        if df is None or df.height == 0:
            m5_cache[code] = None
            return None
        if len(m5_cache) >= 96:
            m5_cache.pop(next(iter(m5_cache)))
        byday: dict[str, list[dict]] = {}
        for r in df.sort("date").to_dicts():
            byday.setdefault(r["date"][:10], []).append(r)
        m5_cache[code] = byday
        return byday

    def log(code, t, side, price_hfq, vol, fee, ttype, gid, reason,
            pnl=None, tag="", open_time=""):
        seq["n"] += 1
        row = code_day_row(ctx, code, t[:10])
        factor = float(row[_IDX_FACTOR]) if row else 1.0
        raw = round(price_hfq / factor, 4)
        trades.append({
            "trade_id": seq["n"], "code": code,
            "name": name_map.get(code, code), "time": t, "side": side,
            "price": raw, "hfq_price": round(price_hfq, 4), "volume": int(vol),
            "amount": round(raw * vol, 2), "fee": round(fee, 2),
            "type": ttype, "group_id": gid, "reason": reason,
            "pnl": (round(pnl, 2) if pnl is not None else None),
            "tag": tag, "open_time": open_time, "t_mode": None})

    def equity_now(day: str) -> float:
        mv = 0.0
        for c, pos in holdings.items():
            row = code_day_row(ctx, c, day)
            if row:
                mv += pos.vol * float(row[_IDX_CLOSE_HFQ]) / float(row[_IDX_FACTOR])
        return cash + mv

    def budget_for(day: str) -> float:
        eq = equity_now(day)
        b = eq * float(p["base_pct"]) / 100.0
        if ctx.gate.get(day, (False, False))[1]:
            b *= float(p["euphoria_scale"])
        b = min(b, eq * max_pos_pct / 100.0, cash * 0.98)
        return b

    def do_buy(code, bar, kind, budget, reason) -> bool:
        nonlocal cash
        day = bar["date"][:10]
        row = code_day_row(ctx, code, day)
        if row is None:
            return False
        factor = float(row[_IDX_FACTOR])
        if kind == "dban":
            exec_hfq = float(row[_IDX_LIMIT]) * factor * (1 + broker.slippage_pct)
        else:
            exec_hfq = broker.buy_price(bar["open"], float(bar["volume"] or 0), 0)
        raw = exec_hfq / factor
        vol = Broker.lots_for_amount(budget, raw)
        vol = broker.cap_volume(vol, float(bar["volume"] or 0))
        if vol <= 0:
            return False
        fee = broker.buy_fee(raw * vol)
        cash -= raw * vol + fee
        gid = seq["n"] + 1
        holdings[code] = _Pos(code=code, vol=vol, hfq_cost=exec_hfq, raw_cost=raw,
                              open_fee=fee, vol0=vol, open_time=bar["date"],
                              sellable=ctx.next_day.get(day),
                              first_hfq=exec_hfq, group_id=gid)
        log(code, bar["date"], "buy", exec_hfq, vol, fee, "开仓", gid,
            reason, tag="开仓")
        return True

    def do_sell(code, bar, vol_want, ttype, reason) -> bool:
        """返回是否成交；T+1 未到 / 一字跌停 -> False（挂单保留次根重试）。"""
        nonlocal cash
        pos = holdings.get(code)
        if pos is None:
            return True
        day = bar["date"][:10]
        if pos.sellable and day < pos.sellable:
            return False
        factor = float(row_factor(code, day))
        row = code_day_row(ctx, code, day)
        lpct = Broker.limit_pct(code, st_map.get(code, False), day)
        limit_dn = round(float(row[_IDX_PREV_CLOSE]) * (1 - lpct) + 1e-9, 2)
        raw_open = bar["open"] / factor
        if (bar["open"] == bar["low"] == bar["close"]
                and abs(raw_open - limit_dn) <= _LIMIT_TOL):
            return False
        vol = pos.vol if vol_want is None else min(int(vol_want), pos.vol)
        vol = broker.cap_volume(vol, float(bar["volume"] or 0))
        if vol <= 0:
            return False
        exec_hfq = broker.sell_price(bar["open"], float(bar["volume"] or 0), vol)
        raw = exec_hfq / factor
        fee = broker.sell_fee(raw * vol)
        cash += raw * vol - fee
        pnl = vol * (raw - pos.raw_cost) - fee \
            - pos.open_fee * (vol / pos.vol0)
        log(code, bar["date"], "sell", exec_hfq, vol, fee, ttype,
            pos.group_id, reason, pnl=pnl, tag="开仓", open_time=pos.open_time)
        if vol >= pos.vol:
            holdings.pop(code, None)
        else:
            pos.vol -= vol
            pos.reductions += 1
        return True

    def row_factor(code, day) -> float:
        row = code_day_row(ctx, code, day)
        return float(row[_IDX_FACTOR]) if row else 1.0

    def day_st(code, day) -> dict:
        key = (code, day)
        st = day_state.get(key)
        if st is None:
            st = {"hi": 0.0, "touched": False, "open": None, "reclaimed": False}
            day_state[key] = st
        return st

    et = str(p.get("entry_type") or "all")
    on = ({"dban", "dip", "gap", "yin"} if et == "all" else {et})
    top_n = max(1, int(p["top_n"]))
    cutoff = str(p.get("entry_cutoff") or "14:30")

    for day in days:
        gate_off, euphoria = ctx.gate.get(day, (False, False))
        gate_days[day] = gate_off
        opens_today = 0
        cands = [] if gate_off else ctx.candidates.get(day, [])
        cand_map = dict(cands)
        active = set(holdings) | set(cand_map) | set(pending)
        prev = prev_day_map.get(day, "")
        if not halted["on"] and "yin" in on and prev:
            for code in ctx.yin_confirms.get(prev, []):
                if code not in holdings and code not in pending:
                    pending[code] = _Pending(side="buy", kind="yin",
                                             reason="首阴次日，买在分歧")

        bars_by_code: dict[str, Optional[list[dict]]] = {}
        for code in active:
            byday = m5_bars(code)
            bars_by_code[code] = (byday or {}).get(day)
        timeline = sorted({b["date"] for bs in bars_by_code.values() if bs
                           for b in bs})

        for t in timeline:
            for code in list(active):
                bs = bars_by_code.get(code)
                if not bs:
                    continue
                bar = next((b for b in bs if b["date"] == t), None)
                if bar is None:
                    continue
                row = code_day_row(ctx, code, day)
                if row is None:
                    continue
                limit_raw = float(row[_IDX_LIMIT] or 0)
                ma5_hfq = row[9]
                factor = float(row[_IDX_FACTOR])
                limit_hfq = limit_raw * factor
                o, h, c = bar["open"], bar["high"], bar["close"]
                one_word_bar = (o == h == bar["low"] == c)
                st = day_st(code, day)
                if st["open"] is None:
                    st["open"] = o
                st["hi"] = max(st["hi"], h)
                if h >= limit_hfq - _LIMIT_TOL and not one_word_bar:
                    st["touched"] = True

                pd = pending.get(code)
                if pd is not None:
                    if pd.side == "sell":
                        pos = holdings.get(code)
                        vol_want = (None if pd.reduce_pct is None or pos is None
                                    else _reduce_vol(pos, pd.reduce_pct))
                        if do_sell(code, bar, vol_want, pd.kind, pd.reason):
                            pending.pop(code, None)
                    else:
                        if not halted["on"] and opens_today < top_n:
                            budget = pd.budget if pd.budget > 0 else budget_for(day)
                            if budget >= 5000 and do_buy(code, bar, pd.kind,
                                                         budget, pd.reason):
                                opens_today += 1
                        pending.pop(code, None)
                    continue

                pos = holdings.get(code)
                if pos is not None:
                    if c <= pos.first_hfq * (1 - stop_pct / 100.0):
                        pending[code] = _Pending(side="sell", kind="止损",
                                                 reason=f"固定止损{stop_pct:g}%",
                                                 tag="开仓")
                        continue
                    if ma5_hfq is not None and c < ma5_hfq:
                        pending[code] = _Pending(side="sell", kind="清仓",
                                                 reason="跌破5日线清仓")
                        continue
                    if (not one_word_bar and c >= limit_hfq - _LIMIT_TOL
                            and pos.reductions < int(p["pyr_max"])):
                        pending[code] = _Pending(side="sell", kind="减仓",
                                                 reason=("涨停晋级金字塔减仓"
                                                         f"（{pos.reductions + 1}"
                                                         f"/{p['pyr_max']}）"),
                                                 reduce_pct=float(p["pyr_step"]))
                        continue
                    continue

                if halted["on"]:
                    # 熔断语义对齐 runner：净值企稳（回撤不再扩大）后，
                    # 首个开仓信号解除熔断；未企稳继续拦截
                    if not halted["stable"]:
                        continue
                    halted["on"] = False
                    halted["stable"] = False
                if gate_off or opens_today >= top_n or len(holdings) >= max_hold:
                    continue
                kinds = cand_map.get(code)
                if not kinds or t[11:16] >= cutoff or not row[_IDX_LIMIT]:
                    continue
                budget = budget_for(day)
                if budget < 5000:
                    continue
                if "dban" in kinds and st["touched"] and not one_word_bar \
                        and (str(p["dban_fill"]) == "touch"
                             or c < limit_hfq - _LIMIT_TOL):
                    if do_buy(code, bar, "dban", budget, "打板，龙头分歧"):
                        opens_today += 1
                    continue
                if "dip" in kinds and st["touched"] and st["hi"] > 0:
                    lo = st["hi"] * (1 - float(p["dip_pb_max"]) / 100)
                    hi_pb = st["hi"] * (1 - float(p["dip_pb_min"]) / 100)
                    if lo <= c <= hi_pb:
                        pending[code] = _Pending(side="buy", kind="dip",
                                                 reason="分时低吸，买在分歧",
                                                 budget=budget)
                        opens_today += 1
                        continue
                if "gap" in kinds and not st["reclaimed"] and st["open"] is not None:
                    prev_close = float(row[_IDX_PREV_CLOSE])
                    if prev_close > 0 and st["open"] / factor <= \
                            prev_close * (1 - float(p["gap_down_min"]) / 100) \
                            and c > st["open"]:
                        st["reclaimed"] = True
                        pending[code] = _Pending(side="buy", kind="gap",
                                                 reason="竞价收复，低开承接",
                                                 budget=budget)
                        opens_today += 1

        # ---- 日终：爆量滞涨 / 熔断 / 净值 ----
        for code, pos in list(holdings.items()):
            row = code_day_row(ctx, code, day)
            if row is None or code in pending:
                continue
            vma = row[_IDX_VOL_MA20]
            vol_ratio = (float(row[_IDX_VOL]) / float(vma)) if vma else 0.0
            prev_close = float(row[_IDX_PREV_CLOSE])
            pct = (float(row[7]) / prev_close - 1) if prev_close else 0.0
            if vol_ratio >= float(p["vol_burst_max"]) \
                    and pct <= float(p["stall_gain_max"]) / 100:
                pending[code] = _Pending(side="sell", kind="清仓",
                                         reason="爆量滞涨清仓")
        eq = equity_now(day)
        peak["v"] = max(peak["v"], eq)
        dd = eq / peak["v"] - 1
        equity_curve.append({"date": day, "equity": round(eq, 2),
                             "adjusted_equity": round(eq, 2),
                             "drawdown": round(dd, 4),
                             "position_ratio": round(1 - cash / eq, 4)
                             if eq > 0 else 0.0})
        if dd * 100 <= -abs(dd_breaker) and not halted["on"]:
            halted["on"] = True
            halted["trough"] = eq
            halted["stable"] = False
            for code in holdings:
                pending.setdefault(code, _Pending(side="sell", kind="清仓",
                                                  reason="回撤熔断清仓"))
        if halted["on"]:
            # 对齐 runner：熔断中创新低重置企稳；不再创新低（含空仓横盘）即企稳；
            # 回撤修复到阈值以内直接解除
            if eq < halted["trough"]:
                halted["trough"] = eq
                halted["stable"] = False
            else:
                halted["stable"] = True
            if dd * 100 < abs(dd_breaker):
                halted["on"] = False
                halted["stable"] = False
        for key in [k for k in day_state if k[1] != day]:
            day_state.pop(key)

    return _report(cfg, p, days, trades, equity_curve, gate_days, initial)


def _reduce_vol(pos: _Pos, pct: float) -> int:
    vol = int(pos.vol * pct / 100.0 // 100) * 100
    if vol >= 100:
        return vol
    return pos.vol if pos.vol <= 100 else 100


def _report(cfg: dict, p: dict, days: list[str], trades: list[dict],
            equity_curve: list[dict], gate_days: dict[str, bool],
            initial: float) -> dict:
    eq = [e["equity"] for e in equity_curve]
    final = eq[-1] if eq else initial
    total_return = final / initial - 1
    n_years = max(len(eq), 1) / 244.0
    rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
    sd = statistics.stdev(rets) if len(rets) > 2 else 0.0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(244)) if sd > 0 else 0.0
    downside = [r for r in rets if r < 0]
    dsd = statistics.stdev(downside) if len(downside) > 2 else 0.0
    sortino = (statistics.mean(rets) / dsd * math.sqrt(244)) if dsd > 0 else 0.0
    peak, mdd = initial, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rounds: dict[int, float] = {}
    for t in trades:
        if t["side"] == "sell" and t["pnl"] is not None:
            rounds[t["group_id"]] = rounds.get(t["group_id"], 0.0) + t["pnl"]
    wins = list(rounds.values())
    win_rate = (sum(1 for w in wins if w > 0) / len(wins)) if wins else 0.0
    gains = [w for w in wins if w > 0]
    losses = [-w for w in wins if w <= 0]
    plr = ((sum(gains) / len(gains)) / (sum(losses) / len(losses))
           if gains and losses else 0.0)
    sells = [t for t in trades if t["side"] == "sell"]
    m = {
        "total_return": round(total_return, 6),
        "annual_return": (round((1 + total_return) ** (1 / max(n_years, 0.01)) - 1, 6)
                          if total_return > -1 else -1.0),
        "max_drawdown": round(mdd, 6),
        "sharpe": round(sharpe, 4), "sortino": round(sortino, 4),
        "calmar": round(total_return / mdd, 4) if mdd < 0 else 0.0,
        "win_rate": round(win_rate, 6),
        "profit_loss_ratio": round(plr, 4),
        "total_trades": len(trades),
        "total_pnl": round(sum(t["pnl"] or 0 for t in sells), 2),
        "avg_hold_days": None,
    }
    rep = {
        "engine_version": "dragon_dip_minute_v1",
        "name": cfg.get("name"), "config": cfg, "params": p,
        "metrics": m, "equity_curve": equity_curve,
        "monthly_returns": monthly_returns(equity_curve, initial),
        "trade_log": trades, "position_snapshots": [],
        "gate_days": gate_days,
    }
    try:
        from .runner import _attach_benchmark
        _attach_benchmark(rep, None)
    except Exception:
        pass
    return rep
