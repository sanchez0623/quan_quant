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
from . import momentum_core as mc
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
    # ---- 做T机制重构（T_REFACTOR）：双止损 + 回补纪律 + 时点规律 ----
    "t_mode": "grid",          # grid=网格(双止损)/discipline=回补纪律/time=时点规律/off=关闭做T
    "t_debt_max_days": 3,      # 债务时限（交易日）：超过未回补 -> 作废转正式减仓（默认3，防崩盘接飞刀）
    "t_max_chase_pct": 3.0,    # 追回价格上限（%）：买回价 > 卖出均价×(1+N%) -> 不追
    "reentry_discount": 1.0,   # 回补限价折让（%）：discipline 模式下卖出价下方 N% 才回补
    # ---- 动态选股（universe_auto）：分段滚动重选 ----
    "universe_auto": False,    # 开启后 universe 留空，池子由动量预筛自动生成并按需重选
    "auto_idle_days": 5,       # 全空仓持续 N 个交易日 -> 触发重选
    "auto_top_x": 30,          # 每次预筛取前 x 只
    "auto_above_ma": 60,       # 站上均线锚周期（60 对齐 momentum_t / 20 对齐 momentum_slot）
    "auto_with_accel": False,  # 动量分叠加加速度项（对齐 momentum_slot）
    "auto_min_rps": None,      # 全市场 RPS 分位下限（0~100，None=不启用）
}

# universe_auto 仅对动量系策略开放（其建仓门槛与预筛口径同源）
AUTO_STRATEGIES = ("momentum_t", "momentum_slot")


def _shift_back(d: str, trading_days: int) -> str:
    """交易日数 -> 自然日近似（7/5）并留缓冲"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return (dt - timedelta(days=int(trading_days * 1.5) + 7)).strftime("%Y-%m-%d")


def _daily_atr(bar: dict, risk_cfg) -> float | None:
    """日线口径 ATR（后复权绝对值）。

    分钟线数据下 atr{N} 是「N 根分钟K线」的波幅，比真实日波动窄一个数量级。
    策略产出的 atr_pct = d_atr / d_close 是日线口径（且已是 T-1 可得语义，无未来函数），
    按当前后复权收盘价折算即得日线 ATR 绝对值。缺失时退回原口径。"""
    ap = bar.get("atr_pct")
    if ap:
        close = bar.get("close")
        if close:
            try:
                return float(ap) * float(close)
            except (TypeError, ValueError):
                pass
    return bar.get(f"atr{risk_cfg.atr_period}") or bar.get("d_atr") or bar.get("atr")


def _add_adaptive_cols(prepared: dict, risk_cfg) -> dict:
    """为自适应止损生成规范化列：adaptive_ma / adaptive_slope / adaptive_vol_q。

    分钟线数据下直接算 MA{N} 得到的是「N 根分钟K线」而非「N 日」，
    因此优先复用策略已产出的日线级特征（ma_slow / slope / atr_pct），
    缺失时才按收盘价自行计算，保证日线/分钟线两种口径都有正确语义。"""
    n = max(2, int(risk_cfg.adaptive_trend_ma))
    sn = max(1, int(risk_cfg.adaptive_slope_n))
    mode = risk_cfg.adaptive
    out = {}
    for code, df in prepared.items():
        cols = list(df.columns)
        exprs = []
        if mode == "trend":
            if "ma_slow" in cols and n == 60:
                # 策略已产出日线级 MA（momentum_t 的 trend_ma 默认 60）
                exprs.append(pl.col("ma_slow").alias("adaptive_ma"))
                if "slope" in cols:
                    exprs.append(pl.col("slope").alias("adaptive_slope"))
                else:
                    exprs.append((pl.col("ma_slow") - pl.col("ma_slow").shift(sn))
                                 .alias("adaptive_slope"))
            else:
                exprs.append(pl.col("close").rolling_mean(n, min_samples=max(2, n // 2))
                             .alias("adaptive_ma"))
                exprs.append((pl.col("close").rolling_mean(n, min_samples=max(2, n // 2))
                              - pl.col("close").rolling_mean(n, min_samples=max(2, n // 2))
                              .shift(sn)).alias("adaptive_slope"))
        elif mode == "vol":
            wn = max(20, int(risk_cfg.adaptive_vol_n))
            # ATR 占价格比；策略已产出 atr_pct 时复用，否则现算。
            # 注意：中间列必须单独 with_columns，不能与引用它的表达式同批（polars 不支持同批引用新列）
            if "atr_pct" in cols:
                df = df.with_columns(pl.col("atr_pct").alias("_ap"))
            else:
                atr_col = f"atr{risk_cfg.atr_period}"
                if atr_col in cols:
                    df = df.with_columns((pl.col(atr_col) / pl.col("close")).alias("_ap"))
            if "_ap" in df.columns:
                mean = pl.col("_ap").rolling_mean(wn, min_samples=max(5, wn // 4))
                std = pl.col("_ap").rolling_std(wn, min_samples=max(5, wn // 4))
                # z-score 线性映射到 [0,1] 近似分位：±2σ 对应 0/1，避免滚动分位的高开销
                q = (0.5 + (pl.col("_ap") - mean) / (4 * std)).fill_nan(0.5).clip(0.0, 1.0)
                df = df.with_columns(q.alias("adaptive_vol_q")).drop("_ap")
        if exprs:
            df = df.with_columns(exprs)
        out[code] = df
    return out


def run_backtest(config: dict, data_dir: Optional[str] = None,
                 progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    """config: 契约 POST /api/backtests 请求体（params 已填默认值）。返回完整 report dict。"""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if v is not None})
    if cfg.get("universe_auto"):
        return _run_auto_segments(cfg, data_dir, progress_cb)
    return _run_one(cfg, data_dir, progress_cb)


def _run_one(cfg: dict, data_dir: Optional[str] = None,
             progress_cb: Optional[Callable[[float, str], None]] = None,
             init_withdraw: Optional[dict] = None) -> dict:
    """单段静态股票池回测（cfg 已合并 DEFAULTS；init_withdraw 用于分段续跑时
    继承月度出金记账状态，保证跨段出金护栏与当月已提额连续）。"""
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

    # 为 ATR / ATR移动止损模式预计算 ATR 列
    if risk_cfg.stop_loss_mode in ("atr", "atr_trailing"):
        atr_n = risk_cfg.atr_period
        prepared = {c: add_atr(df, atr_n, name=f"atr{atr_n}")
                    for c, df in prepared.items()}
    # 自适应止损：预计算趋势/波动判定列（列名规范化，与具体策略解耦）
    if risk_cfg.stop_loss_mode == "atr_trailing" and risk_cfg.adaptive != "off":
        prepared = _add_adaptive_cols(prepared, risk_cfg)

    return _simulate(cfg, prepared, params, risk_cfg, data_dir, progress_cb,
                     init_withdraw=init_withdraw)


# ------------------------------------------------------------------
# 动态选股（universe_auto）：分段滚动重选
# ------------------------------------------------------------------

def _run_auto_segments(cfg: dict, data_dir, progress_cb) -> dict:
    """动态股票池分段滚动重选。

    触发条件：全空仓持续 auto_idle_days 个交易日 -> 以触发日收盘为基准（T-1，
    无后视镜）重跑动量预筛；旧池退役、新池自次一交易日起接管。
    触发时必然无持仓，段间只需传递现金与月度出金记账状态；每段即一次普通
    静态池回测，触发日之后旧池的交易按语义丢弃（重选即退役）。
    空池（全市场无票过门槛，如熊市底部）保持空仓现金推进，直到市场重新出现
    符合门槛的股票再开新段；绝不硬买。
    """
    if cfg["strategy_id"] not in AUTO_STRATEGIES:
        raise RuntimeError(
            f"universe_auto 仅支持策略 {AUTO_STRATEGIES}，当前: {cfg['strategy_id']}")
    start, end = cfg["start_date"], cfg["end_date"]
    idle_n = max(1, int(cfg.get("auto_idle_days") or 5))
    top_x = max(1, int(cfg.get("auto_top_x") or 30))
    min_rps = cfg.get("auto_min_rps")
    wd_base = float(cfg.get("monthly_withdraw_base") or 0)
    pick_p = mc.pick_params(above_ma=int(cfg.get("auto_above_ma") or 60),
                            with_accel=bool(cfg.get("auto_with_accel")))

    # ---- 1) 全市场日线特征：一次构建，全部段共用（窗口含特征最长回看）----
    if progress_cb:
        progress_cb(2.0, "计算全市场动量特征…")
    mf = mc.market_features(data_dir=data_dir, window_start=_shift_back(start, 280),
                            window_end=end, p=pick_p)
    as_of = mc.as_of_before(mf, start)
    if as_of is None:
        raise RuntimeError(f"无后视镜基准日缺失：{start} 之前无行情数据")
    picked = mc.select_top(mf, as_of, top_x, min_rps)
    if picked.height == 0:
        raise RuntimeError(f"初始池为空：基准日 {as_of} 全市场无符合动量趋势条件的股票")

    # ---- 2) 段循环 ----
    acc: dict = {"trades": [], "equity": [], "snaps": [], "cycles": [],
                 "rejects": [], "wlog": [], "commission": 0.0}
    seg_infos: list[dict] = []
    seg_no, seg_start = 0, start
    seg_universe = picked["code"].to_list()
    carry_cash = float(cfg["initial_capital"])
    carry_w: Optional[dict] = None
    final_debts: list[dict] = []

    while True:
        seg_no += 1
        seg_cfg = dict(cfg)
        seg_cfg["universe"] = list(seg_universe)
        seg_cfg["start_date"] = seg_start
        seg_cfg["initial_capital"] = carry_cash
        seg_cfg["name"] = f"{cfg.get('name') or '回测'}·段{seg_no}"
        if progress_cb:
            progress_cb(max(3.0, min(95.0, 100.0 * _day_ratio(seg_start, start, end))),
                        f"段{seg_no}：{seg_start} 起 {len(seg_universe)} 只")
        rep = _run_one(seg_cfg, data_dir, None, init_withdraw=carry_w)
        trig = _find_refresh_point(rep, idle_n)
        info = {"seg": seg_no, "start": seg_start, "as_of": as_of,
                "universe": list(seg_universe),
                "picked": _picked_rows(picked, data_dir)}
        if trig is None:
            _accumulate_segment(acc, rep, seg_no, cutoff=None)
            info["end"] = end
            final_debts = rep.get("t_open_debts") or []
            seg_infos.append(info)
            break
        # 触发重选：本段截断到触发日（其后旧池交易丢弃），旧池退役
        _accumulate_segment(acc, rep, seg_no, cutoff=trig)
        carry_cash = _equity_at(rep, trig)
        carry_w = _summarize_withdraw(
            [e for e in ((rep.get("withdrawal") or {}).get("log") or [])
             if e.get("date", "") <= trig], wd_base)
        as_of = trig
        info["end"] = trig
        info["trigger_day"] = trig
        info["trigger_reason"] = f"全空仓持续{idle_n}个交易日"
        picked = mc.select_top(mf, as_of, top_x, min_rps)
        info["next_picked"] = _picked_rows(picked, data_dir)
        seg_infos.append(info)
        nxt = mc.next_after(mf, trig)
        if nxt is None:
            break
        if picked.height == 0:
            # 空池：现金推进到下一个能选出票的交易日（或回测结束）
            resume = _next_pickable_day(mf, nxt, end, top_x, min_rps)
            _fill_idle(acc, mf, nxt, resume, carry_cash,
                       float(carry_w.get("total") or 0.0))
            if resume is None:
                break
            as_of = resume
            picked = mc.select_top(mf, as_of, top_x, min_rps)
            seg_start = mc.next_after(mf, resume)
            if seg_start is None or seg_start >= end:
                break
            seg_universe = picked["code"].to_list()
            continue
        seg_start = nxt
        seg_universe = picked["code"].to_list()

    # ---- 3) 拼接最终 report：重排 trade_id / 重算 drawdown / 重算 metrics ----
    for i, t in enumerate(acc["trades"], 1):
        t["trade_id"] = i
    peak = None
    for e in acc["equity"]:
        adj = e.get("adjusted_equity", e["equity"])
        peak = adj if peak is None else max(peak, adj)
        e["drawdown"] = round(adj / peak - 1, 6) if peak > 0 else 0.0
    w_summary = _summarize_withdraw(acc["wlog"], wd_base)
    initial = float(cfg["initial_capital"])
    end_equity = acc["equity"][-1]["equity"] if acc["equity"] else carry_cash
    metrics = build_metrics(acc["trades"], acc["equity"], initial, end_equity,
                            acc["commission"],
                            t_cycle_pnls=[float(c["pnl"]) for c in acc["cycles"]],
                            t_cycle_records=acc["cycles"],
                            t_open_debts=final_debts, withdrawn=w_summary,
                            wd_base=wd_base,
                            completed_months=len({e["date"][:7] for e in acc["equity"]}))
    if progress_cb:
        progress_cb(100, "回测完成（动态选股）")
    report = {
        "name": cfg.get("name", ""),
        "config": cfg,
        "engine_version": "t_refactor_v1",
        "universe_auto": True,
        "auto_segments": seg_infos,
        "metrics": metrics,
        "equity_curve": acc["equity"],
        "monthly_returns": monthly_returns(acc["equity"], initial),
        "trade_log": acc["trades"],
        "position_snapshots": acc["snaps"],
        "withdrawal": w_summary,
        "t_open_debts": final_debts,
        "t_reject_events": acc["rejects"],
    }
    if cfg.get("task_id"):
        report["task_id"] = cfg["task_id"]
    return report


def _find_refresh_point(rep: dict, idle_n: int) -> Optional[str]:
    """扫描段内持仓快照，返回第一个「连续空仓达 idle_n 个交易日」的触发日；
    触发日之后段内已无交易日（回测自然结束）时返回 None。"""
    snaps = rep.get("position_snapshots") or []
    idle = 0
    for s in snaps:
        if not s.get("positions"):
            idle += 1
            if idle >= idle_n:
                return s["date"] if s["date"] < snaps[-1]["date"] else None
        else:
            idle = 0
    return None


def _accumulate_segment(acc: dict, rep: dict, seg_no: int,
                        cutoff: Optional[str]) -> None:
    """把单段结果并入累积器；cutoff 给定时截断到该日（丢弃其后交易）。"""
    trades = rep.get("trade_log") or []
    if cutoff:
        trades = [t for t in trades if t["time"][:10] <= cutoff]
    for t in trades:
        t = dict(t)
        t["seg"] = seg_no
        t["group_id"] = (t.get("group_id") or 0) + (seg_no - 1) * 10000  # 跨段隔离建仓组
        acc["trades"].append(t)
    acc["commission"] += sum(float(t.get("fee") or 0.0) for t in trades)
    equity = rep.get("equity_curve") or []
    snaps = rep.get("position_snapshots") or []
    if cutoff:
        equity = [e for e in equity if e["date"] <= cutoff]
        snaps = [s for s in snaps if s["date"] <= cutoff]
    acc["equity"].extend(equity)
    acc["snaps"].extend(snaps)
    cycles = rep.get("t_cycle_records") or []
    if cutoff:
        cycles = [c for c in cycles if c.get("buy_date", "") <= cutoff]
    acc["cycles"].extend(cycles)
    rejects = rep.get("t_reject_events") or []
    if cutoff:
        rejects = [r for r in rejects if r.get("date", "") <= cutoff]
    acc["rejects"].extend(rejects)


def _equity_at(rep: dict, day: str) -> float:
    """触发日收盘的总资产（触发时全空仓，即现金）"""
    eq = [e for e in (rep.get("equity_curve") or []) if e["date"] <= day]
    return float(eq[-1]["equity"]) if eq else 0.0


def _summarize_withdraw(log: list[dict], wd_base: float) -> dict:
    """按出金流水重建汇总（total/months 等），供跨段传递与最终报告"""
    months: dict[str, float] = {}
    total = t_profit = topup = shortfall = recover = 0.0
    for e in log:
        a = float(e.get("amount") or 0.0)
        m = e.get("month") or (e.get("date") or "")[:7]
        months[m] = months.get(m, 0.0) + a
        total += a
        t = e.get("type")
        if t == "t_profit":
            t_profit += a
        elif t == "month_topup":
            topup += a
        elif t == "shortfall":
            shortfall += a
        elif t == "shortfall_recover":
            recover += a
    return {"monthly_base": round(wd_base, 2), "total": round(total, 2),
            "t_profit": round(t_profit, 2), "month_topup": round(topup, 2),
            "shortfall": round(shortfall, 2), "recover": round(recover, 2),
            "months": {m: round(v, 2) for m, v in months.items()},
            "log": log}


def _next_pickable_day(mf, from_day: str, end: str, top_x: int,
                       min_rps) -> Optional[str]:
    """from_day 起第一个能选出票的交易日（空池段的恢复日）；找不到返回 None"""
    for d in mf.calendar:
        if d < from_day:
            continue
        if d > end:
            return None
        if mc.select_top(mf, d, top_x, min_rps).height:
            return d
    return None


def _fill_idle(acc: dict, mf, from_day: str, to_day: Optional[str],
               cash: float, w_total: float) -> None:
    """空池段：现金恒定推进净值/快照曲线（drawdown 由拼接层统一重算）"""
    d = from_day
    while d and (to_day is None or d <= to_day):
        acc["equity"].append({"date": d, "equity": round(cash, 2),
                              "adjusted_equity": round(cash + w_total, 2),
                              "drawdown": 0.0, "position_ratio": 0.0})
        acc["snaps"].append({"date": d, "cash": round(cash, 2),
                             "market_value": 0.0, "positions": []})
        d = mc.next_after(mf, d)


def _picked_rows(picked: pl.DataFrame, data_dir) -> list[dict]:
    """预筛结果 -> [{rank, code, name, score, rps}]（报告/选股详情展示用）"""
    if picked is None or picked.height == 0:
        return []
    codes = picked["code"].to_list()
    names = _stock_names(codes, data_dir)
    return [{"rank": int(r["rank"]), "code": r["code"],
             "name": names.get(r["code"], r["code"]),
             "score": round(float(r["score"]), 4),
             "rps": (round(float(r["rps"]) * 100, 1)
                     if r.get("rps") is not None else None)}
            for r in picked.to_dicts()]


def _day_ratio(d: str, start: str, end: str) -> float:
    """自然日进度占比（分段进度显示用）"""
    try:
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        c = datetime.strptime(d, "%Y-%m-%d")
        span = max(1, (e - s).days)
        return max(0.0, min(1.0, (c - s).days / span))
    except Exception:  # noqa: BLE001
        return 0.0


# ------------------------------------------------------------------

def _filter_st(universe: list[str], exclude_st: bool, data_dir) -> list[str]:
    """剔除 ST 股与退市股（ST 按 exclude_st 开关；退市股无条件剔除）"""
    basic = store.read_stock_basic(data_dir)
    if basic is None:
        return universe
    excluded = set()
    if exclude_st:
        excluded |= set(basic.filter(pl.col("st")).select("code").to_series().to_list())
    if "delisted" in basic.columns:
        excluded |= set(basic.filter(pl.col("delisted")).select("code").to_series().to_list())
    if not excluded:
        return universe
    filtered = [c for c in universe if c not in excluded]
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
              risk_cfg: RiskConfig, data_dir, progress_cb,
              init_withdraw: Optional[dict] = None) -> dict:
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
    # ---- 做T机制（T_REFACTOR）：双止损/回补纪律/时点规律 参数（策略 schema 优先，DEFAULTS 兜底）----
    t_mode = str(params.get("t_mode") or cfg.get("t_mode") or "grid")
    t_debt_max_days = max(1, int(params.get("t_debt_max_days") if params.get("t_debt_max_days") is not None else (cfg.get("t_debt_max_days") or 3)))
    t_max_chase_pct = float(params.get("t_max_chase_pct") if params.get("t_max_chase_pct") is not None else (cfg.get("t_max_chase_pct") or 3.0))
    reentry_discount = float(params.get("reentry_discount") if params.get("reentry_discount") is not None else (cfg.get("reentry_discount") or 1.0))

    # ---- 出金配置 ----
    wd_base = float(cfg.get("monthly_withdraw_base") or 0)
    wd_pct = float(cfg.get("t_profit_withdraw_pct") or 0)
    min_t_amount = float(cfg.get("min_t_amount") or 0)
    # 分段续跑：继承此前各段的出金记账（months 当月已提额 / 累计缺口等），
    # 保证跨段月度出金护栏连续；log 由各段自记，最终拼接合并。
    _iw = init_withdraw or {}
    w_state = {"total": float(_iw.get("total") or 0.0),
               "t_profit": float(_iw.get("t_profit") or 0.0),
               "topup": float(_iw.get("topup") or 0.0),
               "shortfall": float(_iw.get("shortfall") or 0.0),
               "recover": float(_iw.get("recover") or 0.0),
               "months": dict(_iw.get("months") or {}),
               "log": []}

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
    t_state: dict[str, dict] = {}      # code -> 做T债务（跨日保留直至回补/到期作废/清仓）
    adds_count: dict[str, int] = {}    # code -> 当前持仓期内加仓次数
    max_adds = int(params.get("max_adds") or 0)
    t_cycle_pnls: list[float] = []     # 已闭环做T周期价差合计（旧周期口径，t_pnl_closed 对照）
    t_cycle_records: list[dict] = []   # 配对口径周期明细 {code, sell_date, buy_date, pnl}
    t_reject_events: list[dict] = []   # 追回/回补被拒事件（审计可见，不污染 trade_log）
    state = {"intraday_trades": {}, "commission_total": 0.0, "trade_seq": 0}  # code -> 当日交易次数

    def _t_state(code: str) -> dict:
        return t_state.setdefault(code, {"sold": 0, "bought": 0,
                                         "sell_amt": 0.0, "buy_amt": 0.0,
                                         "open_day": None, "deadline_day": None,
                                         "sell_trade_ids": []})

    def _advance_trading_day(d: str, n: int):
        "从 d 起推进 n 个交易日（next_day 为交易日映射）"
        cur = d
        for _ in range(n):
            nd = next_day.get(cur)
            if nd is None:
                return None
            cur = nd
        return cur

    def _book_t_cycle(code, day, sell_date, pnl):
        t_cycle_pnls.append(pnl)
        t_cycle_records.append({"code": code, "sell_date": sell_date,
                                "buy_date": day, "pnl": pnl})
        if pnl > 0 and wd_pct > 0:
            amt = min(pnl * wd_pct / 100.0, portfolio.cash)
            if amt > 0:
                portfolio.cash -= amt
                w_state["total"] += amt
                w_state["t_profit"] += amt
                month = day[:7]
                w_state["months"][month] = w_state["months"].get(month, 0.0) + amt
                w_state["log"].append({"month": month, "date": day,
                                       "type": "t_profit", "amount": round(amt, 2)})

    def _reclassify_sells(st, suffix):
        for tid in st.get("sell_trade_ids") or []:
            for entry in trade_log:
                if entry["trade_id"] == tid:
                    entry["tag"] = "减仓"
                    entry["reason"] = (entry.get("reason") or "") + "（" + suffix + "）"
                    break

    def _expire_debt(code, st, day, suffix):
        if st["sold"] > 0 and st["bought"] > 0:
            sell_px_avg = st["sell_amt"] / st["sold"]
            _book_t_cycle(code, day, st["open_day"],
                          sell_px_avg * st["bought"] - st["buy_amt"])
        _reclassify_sells(st, suffix)
        t_state.pop(code, None)

    def _clear_debt_on_close(code):
        st = t_state.pop(code, None)
        if st and st["sold"] > st["bought"] and st.get("sell_trade_ids"):
            _reclassify_sells(st, "清仓：做T债务作废转减仓")

    def _t_rebuy_allowed(st, raw_price, day, code):
        if not st["sold"]:
            return True
        sell_px_avg = st["sell_amt"] / st["sold"]
        if t_mode == "discipline":
            limit = sell_px_avg * (1 - reentry_discount / 100.0)
            ok = raw_price <= limit
            rtype, rsn = "discipline", "回补限价未到"
        else:
            limit = sell_px_avg * (1 + t_max_chase_pct / 100.0)
            ok = raw_price <= limit
            rtype, rsn = "chase", "超追回上限" + str(t_max_chase_pct) + "%"
        if not ok:
            t_reject_events.append({"code": code, "name": names.get(code, code),
                                    "date": day, "type": rtype,
                                    "buy_price": round(raw_price, 4),
                                    "sell_px_avg": round(sell_px_avg, 4),
                                    "reason": rsn})
        return ok

    def _trades_today(code: str) -> int:
        return state["intraday_trades"].get(code, 0)

    # ---- 闭包工具 ----

    def log_trade(code, bar, side, price, volume, fee, ttype, group_id, reason,
                  pnl=None, tag="", open_time="", t_mode=""):
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
            "tag": tag, "open_time": open_time, "t_mode": t_mode or None,
        })

    def execute_sell(code, bar, volume_wanted, ttype, reason, basis_price,
                     only_group: Optional[int] = None):
        """FIFO 平仓；只卖 sellable（T+1）仓位；一字跌停不成交；卖出允许零股。
        basis_price 为原始成交价基准（open/close），滑点与成交量约束在此统一计算。
        only_group: 指定后只平该 group 的仓位（止损/止盈挂单按 group 隔离，
        避免同一股票多 group 同时触发时互相误清、量被拆分）。"""
        day = bar["date"][:10]
        pc = prev_daily_close[code].get(day)
        if broker.is_limit_down(bar, pc, _lpct(code, day)):
            return 0, 0.0
        positions = sorted([p for p in portfolio.positions_of(code)
                            if p.sellable_date and day >= p.sellable_date
                            and (only_group is None or p.group_id == only_group)],
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
            _clear_debt_on_close(code)  # 仓位清零：未还清做T债务作废（卖出利润已按平仓盈亏入账，转减仓语义）
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
                # 追回/回补执行判定（L1 价格止损 / L2 回补纪律），被拒则本次不成交
                if not _t_rebuy_allowed(st, raw_price, day, code):
                    return
                # 债务买回若发生在"该股持仓已清"状态，等于重新建仓，须遵守槽位上限
                if (risk_cfg.max_holdings > 0 and not portfolio.positions_of(code)
                        and len({p.code for p in portfolio.positions}) >= risk_cfg.max_holdings):
                    return
                # 买回做T卖出的筹码（债务跨日保留，直至回补/到期作废）
                amount = min(debt * raw_price, budget)
                vol = broker.lots_for_amount(amount, raw_price)
            elif portfolio.volume_of(code) == 0:
                # 底仓已被止损/清仓：网格买点重建底仓
                tag = "开仓"
            else:
                # 纯正向T：持仓存在但无债务 → 用预算占比逢低加仓
                budget_pct = order.get("budget_pct")
                if budget_pct:
                    budget = min(budget, equity * float(budget_pct) / 100)
                vol = broker.lots_for_amount(budget, raw_price)
        if tag in ("开仓", "加仓"):
            # 槽位管理：无论开仓还是加仓信号，只要该 code 当前不在持仓且槽位已满，
            # 一律拒绝（覆盖"试仓未成交/已清但策略状态机未同步"时加仓补建的情况）
            if risk_cfg.max_holdings > 0:
                held = {p.code for p in portfolio.positions}
                if code not in held and len(held) >= risk_cfg.max_holdings:
                    return  # 持仓只数已达上限（已有持仓的加仓/做T不受影响）
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
            if st["sold"] > 0:
                # 反向T买回：更新债务跟踪
                st["bought"] += vol
                st["buy_amt"] += raw_price * vol
                log_trade(code, bar, "buy", price, vol, fee, "做T", pos.group_id,
                          order.get("reason", "网格买回"), t_mode=t_mode)
                if st["bought"] >= st["sold"]:
                    # 做T周期完成：卖旧与买回的价差即为做T贡献（配对口径 + 逐笔出金）
                    _book_t_cycle(code, day, st["open_day"], st["sell_amt"] - st["buy_amt"])
                    t_state.pop(code, None)
            else:
                # 正向T逢低加仓：不涉及债务，仅记录交易
                log_trade(code, bar, "buy", price, vol, fee, "做T", pos.group_id,
                          order.get("reason", "正向T逢低买入"), t_mode=t_mode)
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
                seq0 = state["trade_seq"]
                sold, fill_price = execute_sell(code, bar, want, "做T",
                                                order.get("reason", "网格卖出"), bar["open"])
                if sold:
                    st = _t_state(code)
                    # 网格高抛/反向T卖出：无条件建立债务（首次卖出 sold==0 也要记，
                    # 否则债务链断裂导致后续买回永不配对、T 统计恒为 0）
                    st["sold"] += sold
                    st["sell_amt"] += (fill_price / float(bar.get("adj_factor") or 1.0)) * sold
                    # 债务记账：首次卖出记录 open_day + deadline_day（到期作废转减仓）
                    if st["open_day"] is None:
                        st["open_day"] = day
                        st["deadline_day"] = _advance_trading_day(day, t_debt_max_days)
                    # 记录本次卖出的 trade_id（到期/清仓时转减仓标注）+ t_mode 标签
                    st["sell_trade_ids"].extend(range(seq0 + 1, state["trade_seq"] + 1))
                    for tid in range(seq0 + 1, state["trade_seq"] + 1):
                        for entry in trade_log:
                            if entry["trade_id"] == tid:
                                entry["t_mode"] = t_mode
                                break
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
            # ATR 口径修正（ATR_DAILY_FIX）：
            # 分钟线数据下 atr{N} 是「N 根分钟K线」的波幅（实测仅 0.65% 量级），
            # 远窄于真实日波动（日线 atr_pct 中位 6.00%，相差约 9 倍）。
            # 直接用会让止损线紧贴买入价、反复扫损。atr_trailing 模式改用日线口径；
            # 旧 atr 模式保持原样，避免历史报告不可比（是否修复待定）。
            if risk_cfg.stop_loss_mode == "atr_trailing":
                atr = _daily_atr(bar, risk_cfg)
            else:
                atr = bar.get(f"atr{risk_cfg.atr_period}") or bar.get("d_atr") or bar.get("atr")
            hit = risk_mgr.check_stop(pos, bar["close"], atr, bar)
            if hit:
                action, reason = hit
                ttype = "止损" if action == "stop_loss" else "止盈"
                if stop_fill == "next_open":
                    pending_stops.setdefault(code, []).append(
                        {"code": code, "pos": pos, "volume": pos.volume,
                         "ttype": ttype, "reason": reason})
                else:
                    execute_sell(code, bar, pos.volume, ttype, reason, bar["close"],
                                 only_group=pos.group_id)

    def execute_pending_stops(code, bar):
        """执行上一bar挂起的止损单（本bar开盘成交，优先级高于策略信号）。
        一字跌停无法成交 -> 顺延到下一bar；仓位已被其它单清掉 -> 作废。
        按挂单记录的 group 隔离平仓：同一股票多 group 同时触发止损/止盈时，
        每张单只清自己对应 group，避免 FIFO 误清其它 group、量被拆分。"""
        queued = pending_stops.get(code)
        if not queued:
            return
        kept = []
        for o in queued:
            if not any(o["pos"] is p for p in portfolio.positions):
                continue  # 该仓位已平（策略/其它止损），作废
            sold, _ = execute_sell(code, bar, o["volume"], o["ttype"], o["reason"],
                                   bar["open"], only_group=o["pos"].group_id)
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
        if day != cur_day:  # 新交易日：重置日内状态（做T债务跨日保留直至回补/到期作废）
            cur_day = day
            state["intraday_trades"] = {}
            # 做T时间止损（L1/L2）：债务超过 t_debt_max_days 交易日未回补 -> 作废转正式减仓
            for code in list(t_state):
                st = t_state[code]
                if st["sold"] > st["bought"] and st["deadline_day"] and day > st["deadline_day"]:
                    _expire_debt(code, st, day, "T债务超时转减仓")

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
    # ---- 期末未闭环债务（配对口径浮亏计提：未回补部分按当前价 mark-to-market）----
    t_open_debts = []
    for code, st in t_state.items():
        if st["sold"] <= st["bought"]:
            continue
        remaining = st["sold"] - st["bought"]
        sell_px_avg = st["sell_amt"] / st["sold"] if st["sold"] else 0.0
        last_price = price_map.get(code) or 0.0
        t_open_debts.append({
            "code": code, "name": names.get(code, code),
            "sell_date": st["open_day"], "remaining": remaining,
            "sell_px_avg": round(sell_px_avg, 4), "last_price": round(last_price, 4),
            "float_pnl": round((sell_px_avg - last_price) * remaining, 2),
        })
    metrics = build_metrics(trade_log, equity_curve, portfolio.initial_cash,
                            end_equity, state["commission_total"],
                            t_cycle_pnls=t_cycle_pnls, t_cycle_records=t_cycle_records,
                            t_open_debts=t_open_debts, withdrawn=w_state,
                            wd_base=wd_base, completed_months=completed_months)
    mret = monthly_returns(equity_curve, portfolio.initial_cash)
    if progress_cb:
        progress_cb(100, "回测完成")

    report = {
        "name": cfg.get("name", ""),
        "config": cfg,
        "engine_version": "t_refactor_v1",   # T_REFACTOR：t_pnl 改配对口径后与旧版不可比（AUDIT B1）
        "metrics": metrics,
        "equity_curve": equity_curve,
        "monthly_returns": mret,
        "trade_log": trade_log,
        "position_snapshots": snapshots,
        "withdrawal": withdrawn_summary,
        "t_cycle_records": t_cycle_records,   # 配对口径周期明细（分段拼接重算 metrics 用）
        "t_open_debts": t_open_debts,
        "t_reject_events": t_reject_events,
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
