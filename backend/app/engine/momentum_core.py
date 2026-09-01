# -*- coding: utf-8 -*-
"""动量趋势公共核心（MOMENTUM_CORE）：条件选股器与 momentum 系策略共用的单一事实来源。

目标：保证「选股器算的分 = 策略认的分」。风险调整动量分、崩溃保护、
横截面排名的公式此前在 momentum_t / momentum_slot 中各有一份重复实现，
现全部收敛到本模块；策略文件只保留逐 bar 状态机与做T层。

分层：
- aggregate_daily()        任意周期K线 -> 日线聚合（策略与全市场路径共用入口）
- daily_feature_core()     单股日线 OHLC -> 趋势/波动/动量特征（策略 _daily_features 内核）
- rank_days()              横截面动量排名 -> code -> 可建仓日集合（T-1 语义）
- market_features()        全市场日线特征长表（条件选股 / universe_auto 动态重选共用）
- select_top()             基准日「门槛 -> RPS -> 排序 -> 取前 x」
- as_of_before/next_after  基准日推进工具（严格早于/晚于指定日）

无后视镜约定：day D 的特征由截至 D 收盘的数据计算；select_top(as_of=D)
只使用 D 收盘信息，调用方须在 D 的**下一交易日**才允许据此建仓（T-1 语义）。
"""
from bisect import bisect_left, bisect_right
from typing import Optional

import polars as pl

from ..data import store
from .datafeed import _attach_adj
from .indicators import _rolling_params, add_atr, add_macd, add_ma

# 动量选股参数（条件选股器与 universe_auto 动态重选共用同一套默认；
# 均线锚/加速度项可覆盖，崩溃保护内置不可关）
DEFAULT_PICK_PARAMS = {
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "slope_n": 5, "atr_period": 14,
    "mom_short": 20, "mom_mid": 60, "mom_long": 120,
    "w_short": 0.5, "w_mid": 0.3,          # w_long = 1 - w_short - w_mid
    "w_accel": 0.3,
    "crash_sigma": 2.0, "crash_vol_n": 60, "crash_abs_cap": 30.0,
    "vol_window": 120, "vol_q_hi": 0.7, "vol_q_lo": 0.3,
    "add_breakout_n": 20,
}

_PICK_SCHEMA = {"rank": pl.UInt32, "code": pl.Utf8,
                "score": pl.Float64, "rps": pl.Float64}


def pick_params(above_ma: int = 60, with_accel: bool = False) -> dict:
    """构造动量选股参数：above_ma=站上均线锚周期（60 对齐 momentum_t / 20 对齐 momentum_slot），
    with_accel=True 时叠加 momentum_slot 的加速度项"""
    p = dict(DEFAULT_PICK_PARAMS)
    p["anchor_n"] = int(above_ma)
    p["with_accel"] = bool(with_accel)
    return p


def aggregate_daily(df: pl.DataFrame) -> pl.DataFrame:
    """任意周期K线 -> 日线聚合：day / d_close(收盘) / d_high / d_low"""
    return (df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
              .group_by("day").agg(pl.col("close").last().alias("d_close"),
                                   pl.col("high").max().alias("d_high"),
                                   pl.col("low").min().alias("d_low"))
              .sort("day"))


def daily_feature_core(daily: pl.DataFrame, p: dict,
                       anchor_key: str = "trend_ma", anchor_name: str = "ma_slow",
                       with_accel: bool = False,
                       keep_close: bool = False) -> pl.DataFrame:
    """单股日线（d_close/d_high/d_low 列）-> 特征表。

    与 momentum_t / momentum_slot 的 _daily_features 完全同口径：
    MACD、均线锚、斜率、ATR%、乖离 bias、多周期风险调整动量分（可选加速度项）、
    σ自适应+绝对上限崩溃保护、波动位置 vol_pos、突破标记。"""
    daily = add_macd(daily, int(p["macd_fast"]), int(p["macd_slow"]),
                     int(p["macd_signal"]), col="d_close")
    daily = add_ma(daily, int(p[anchor_key]), col="d_close", name=anchor_name)
    # add_atr 需要 close/high/low 列名：临时重命名计算后还原
    daily = (add_atr(daily.rename({"d_close": "close", "d_high": "high",
                                   "d_low": "low"}),
                     int(p["atr_period"]), name="d_atr")
             .rename({"close": "d_close", "high": "d_high", "low": "d_low"}))
    daily = daily.with_columns([
        (pl.col(anchor_name) - pl.col(anchor_name).shift(int(p["slope_n"]))).alias("slope"),
        (pl.col("d_atr") / pl.col("d_close")).alias("atr_pct"),
    ])
    slope_n = int(p["slope_n"])
    mom_s, mom_m, mom_l = int(p["mom_short"]), int(p["mom_mid"]), int(p["mom_long"])
    w_s, w_m = float(p["w_short"]), float(p["w_mid"])
    w_l = max(0.0, 1.0 - w_s - w_m)  # 长周期权重 = 1 - 短 - 中
    crash_sigma = float(p["crash_sigma"])
    crash_n = int(p["crash_vol_n"])
    crash_abs = float(p["crash_abs_cap"]) / 100.0
    vol_n = int(p["vol_window"])
    vol_q_hi = float(p["vol_q_hi"])
    vol_q_lo = float(p["vol_q_lo"])
    brk_n = int(p["add_breakout_n"])

    # 日收益率（风险调整与崩溃保护的公共输入）
    daily_ret = pl.col("d_close") / pl.col("d_close").shift(1) - 1
    # 日波动绝对下限：真实市场日 σ 最低约 0.5%（低波银行股），低于此视为无波动，
    # 防止恒定收益序列导致 std 浮点下溢（~1e-16）后除零/误触发
    _vol_floor = 0.005

    def _risk_adj(n: int):
        """风险调整动量：N日涨幅 / N日波动（横截面可比，防高波动假强势）
        波动低于绝对下限时无风险调整必要，退化为原始涨幅"""
        ret = pl.col("d_close") / pl.col("d_close").shift(n) - 1
        vol = daily_ret.rolling_std(n, **_rolling_params(n)) * (n ** 0.5)
        return pl.when(vol > _vol_floor * (n ** 0.5)).then(ret / vol).otherwise(ret)

    mom_s_expr = _risk_adj(mom_s)
    mom_m_expr = _risk_adj(mom_m)
    mom_l_expr = _risk_adj(mom_l)
    if with_accel:
        # 加速度项：短周期跑赢中周期 = 处于加速段（启动期），仅取正向
        accel = (mom_s_expr - mom_m_expr).clip(lower_bound=0.0)
        score_expr = (w_s * mom_s_expr + w_m * mom_m_expr + w_l * mom_l_expr
                      + float(p.get("w_accel", 0.3)) * accel)
    else:
        score_expr = w_s * mom_s_expr + w_m * mom_m_expr + w_l * mom_l_expr
    # σ自适应崩溃保护：近5日涨幅 > crash_sigma × 自身σ√5 -> 动量分作废不入榜
    # （自动适配板块：创业板/科创板 σ 大阈值宽，低波股 σ 小阈值严，无需识别代码前缀）
    # 绝对上限：近5日涨幅 > crash_abs_cap 硬性禁入（σ 阈值作第二道），
    # 防止高波股连板后 σ 被撑大导致自适应阈值放宽、仍被放行满配。
    ret5 = pl.col("d_close") / pl.col("d_close").shift(5) - 1
    vol5 = (daily_ret.rolling_std(crash_n, **_rolling_params(crash_n))
            .clip(lower_bound=_vol_floor) * (5 ** 0.5))

    daily = daily.with_columns([
        # 乖离（以 ATR 为单位）：>0 强上行
        pl.when(pl.col("d_atr") > 0)
          .then((pl.col("d_close") - pl.col(anchor_name)) / pl.col("d_atr"))
          .otherwise(None).alias("bias"),
        score_expr.alias("score"),
        ret5.alias("ret5"),
        vol5.alias("vol5"),
    ])
    # 波动位置 vol_pos ∈ [0,1]：ATR% 相对滚动分位数定档（每只票自适应）。
    # 高于 vol_q_hi 分位 → 1（高波），低于 vol_q_lo 分位 → 0（低波），之间线性；
    # 样本不足/分位数重合（恒定波动）→ None，由状态机按中性 0.5 处理。
    daily = daily.with_columns([
        pl.col("atr_pct").rolling_quantile(vol_q_hi, window_size=vol_n,
                                           **_rolling_params(1)).alias("vq_hi"),
        pl.col("atr_pct").rolling_quantile(vol_q_lo, window_size=vol_n,
                                           **_rolling_params(1)).alias("vq_lo"),
    ])
    daily = daily.with_columns(
        pl.when(pl.col("vq_hi") > pl.col("vq_lo"))
          .then(((pl.col("atr_pct") - pl.col("vq_lo"))
                 / (pl.col("vq_hi") - pl.col("vq_lo"))).clip(0.0, 1.0))
          .otherwise(None).alias("vol_pos"))
    daily = daily.with_columns(
        pl.when(((pl.col("vol5") > 0) & (pl.col("ret5") > crash_sigma * pl.col("vol5")))
                | (pl.col("ret5") > crash_abs))
          .then(pl.lit(None)).otherwise(pl.col("score")).alias("score"))
    daily = daily.with_columns([
        # 突破 N 日新高（金字塔加仓条件）
        (pl.col("d_close") >= pl.col("d_close")
         .rolling_max(brk_n, **_rolling_params(1)).shift(1)).alias("breakout"),
    ])
    # 交易日序号（冷却期计算用）
    cols = ["day", "day_idx", "dif", "dea", anchor_name, "slope", "atr_pct",
            "bias", "score", "vol_pos", "breakout"]
    if keep_close:
        cols.append("d_close")  # 全市场路径：门槛「收盘 > 均线锚」需要后复权收盘价
    return daily.with_row_index("day_idx").select(cols)


def rank_days(feats: dict[str, pl.DataFrame], top_n: int) -> dict[str, set]:
    """每日按动量分排名，返回 code -> 可建仓日集合（T-1 语义）。

    day D 的动量分在 D 收盘后才可知，因此 D 的 top_n 名次只决定
    D 的**下一交易日**（全局交易日并集的次日）是否可建仓。
    """
    rows: list[tuple[str, str, float]] = []
    for code, f in feats.items():
        for day, score in zip(f["day"].to_list(), f["score"].to_list()):
            if score is not None:
                rows.append((day, code, float(score)))
    by_day: dict[str, list] = {}
    for day, code, score in rows:
        by_day.setdefault(day, []).append((score, code))
    # 全局交易日历（各代码日期的并集，升序）-> 次一交易日
    cal = sorted(by_day)
    next_day = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
    out: dict[str, set] = {c: set() for c in feats}
    for day, items in by_day.items():
        nd = next_day.get(day)
        if nd is None:
            continue  # 最后一天无次日，不产生建仓日
        items.sort(reverse=True)
        for _s, code in items[:max(1, top_n)]:
            out.setdefault(code, set()).add(nd)
    return out


# ------------------------------------------------------------------
# 全市场路径（条件选股器 / universe_auto 动态重选）
# ------------------------------------------------------------------

class MarketFeatures:
    """全市场日线特征长表 + 全局交易日历（momentum 选股/重选的查询底座）"""

    def __init__(self, feats: pl.DataFrame, calendar: list[str], params: dict):
        self.feats = feats          # code, day, day_idx, score, macd_ok, above
        self.calendar = calendar    # 升序交易日（数据并集）
        self.params = params


def market_features(data_dir: Optional[str] = None,
                    window_start: Optional[str] = None,
                    window_end: Optional[str] = None,
                    p: Optional[dict] = None) -> MarketFeatures:
    """全市场日线特征长表（后复权口径，与策略同一数据路径）。

    window_start/window_end 限定特征窗口；窗口须覆盖最长回看参数
    （vol_window 120 + mom_long 120 等约 240 交易日）。
    ST 与退市股无条件剔除（与引擎 _filter_st 口径一致）。
    """
    p = p or pick_params()
    daily = store.read_daily(None, data_dir)
    if daily is None or daily.height == 0:
        raise RuntimeError("日线数据为空，请先在数据管理页更新日线")
    daily = daily.select(["code", "date", "open", "high", "low", "close"])
    if window_start:
        daily = daily.filter(pl.col("date") >= window_start)
    if window_end:
        daily = daily.filter(pl.col("date") <= window_end)
    basic = store.read_stock_basic(data_dir)
    if basic is not None and basic.height:
        excl = set(basic.filter(pl.col("st") | pl.col("delisted"))["code"].to_list())
        if excl:
            daily = daily.filter(~pl.col("code").is_in(list(excl)))
    if daily.height == 0:
        raise RuntimeError("特征窗口内无日线数据")
    adj = store.read_adj_factor(None, data_dir)
    daily = _attach_adj(daily.sort(["code", "date"]), adj)

    anchor_name = "ma_anchor"
    parts: list[pl.DataFrame] = []
    for g in daily.partition_by("code"):
        code = g["code"][0]
        # 日线 parquet 无 day 列（date 即交易日，防御性截断），补齐供特征内核使用
        g = g.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
        g = g.rename({"close": "d_close", "high": "d_high", "low": "d_low"})
        f = daily_feature_core(g, p, anchor_key="anchor_n", anchor_name=anchor_name,
                               with_accel=bool(p.get("with_accel")), keep_close=True)
        # 门槛布尔列在单股内算好：MACD 金叉 + 收盘站上均线锚
        # （score 非 None 即已通过崩溃保护；阈值/排序过滤在 select_top 完成）
        f = f.with_columns([
            (pl.col("dif").is_not_null() & pl.col("dea").is_not_null()
             & (pl.col("dif") > pl.col("dea"))).alias("macd_ok"),
            (pl.col("ma_anchor").is_not_null()
             & (pl.col("d_close") > pl.col("ma_anchor"))).alias("above"),
        ])
        parts.append(f.with_columns(pl.lit(code).alias("code"))
                     .select(["code", "day", "day_idx", "score", "macd_ok", "above"]))
    if not parts:
        raise RuntimeError("特征窗口内无可用个股")
    feats = pl.concat(parts).sort(["code", "day"])
    calendar = sorted(feats["day"].unique().to_list())
    return MarketFeatures(feats, calendar, p)


def as_of_before(mf: MarketFeatures, day: str) -> Optional[str]:
    """严格早于 day 的最近交易日（无后视镜基准日）；不存在返回 None"""
    i = bisect_left(mf.calendar, day)
    return mf.calendar[i - 1] if i > 0 else None


def next_after(mf: MarketFeatures, day: str) -> Optional[str]:
    """严格晚于 day 的最近交易日；不存在返回 None"""
    i = bisect_right(mf.calendar, day)
    return mf.calendar[i] if i < len(mf.calendar) else None


# ------------------------------------------------------------------
# 池级趋势开关（POOL_GATE）
# ------------------------------------------------------------------

# 确认天数固定 2（防抖动；不开放为参数，避免新的过拟合旋钮）
POOL_GATE_CONFIRM_DAYS = 2


def _pool_gate_map(day_health: list[tuple[str, float]],
                   enter_th: float) -> dict[str, bool]:
    """池级趋势开关状态机。

    输入按交易日升序的 (day, 健康度)——健康度 = 当日池内动量分>0 的票数占比；
    输出 day -> 是否停开仓（True=抑制）。语义：
    - 触发：健康度连续 POOL_GATE_CONFIRM_DAYS 日 < enter_th -> 停开仓
    - 恢复：健康度连续 2 日 >= enter_th*2（滞回恢复线=触发线×2，内置不开放）
    - 返回值已是 **T-1 对齐**：第 i 日的 bar 只能看见第 i-1 日收盘的状态
      （无后视镜：当日收盘健康度当日不可知），首日视为开启。
    - 中间地带（enter_th ~ 2×enter_th）保持现状（滞回区，防抖动）。
    """
    gates: list[bool] = []
    on = False
    low = 0
    high = 0
    for _day, h in day_health:
        if on:
            high = high + 1 if h >= enter_th * 2 else 0
            if high >= POOL_GATE_CONFIRM_DAYS:
                on = False
        else:
            low = low + 1 if h < enter_th else 0
            if low >= POOL_GATE_CONFIRM_DAYS:
                on = True
        gates.append(on)
    # T-1 对齐：当日 bar 看前一日收盘状态；首日无前日 -> 开启
    out: dict[str, bool] = {}
    for i, (day, _h) in enumerate(day_health):
        out[day] = gates[i - 1] if i > 0 else False
    return out


def pool_gate_column(feats: dict[str, pl.DataFrame], enter_th: float) -> pl.DataFrame:
    """由各股特征表（含 day/score 列）计算池级 gate 表 (day, pool_gate)。

    健康度 = 当日 universe 内动量分>0 的票数 / 有分数票数（剔停牌稀释）。
    供策略 prepare 内 join（T-1 对齐由 _pool_gate_map 保证）。"""
    frames = [f.select(pl.col("day"), pl.col("score")) for f in feats.values()]
    sc = pl.concat(frames)
    daily = (sc.group_by("day")
               .agg([(pl.col("score") > 0).sum().alias("pos"),
                     pl.col("score").is_not_null().sum().alias("n")])
               .with_columns(
                   (pl.col("pos") / pl.col("n").clip(lower_bound=1)).alias("h"))
               .sort("day"))
    gate_map = _pool_gate_map(list(daily.select(["day", "h"]).iter_rows()), enter_th)
    return pl.DataFrame({"day": list(gate_map.keys()),
                         "pool_gate": list(gate_map.values())})


def _empty_pick() -> pl.DataFrame:
    return pl.DataFrame(schema=_PICK_SCHEMA)


def select_top(mf: MarketFeatures, as_of_day: str, top_x: int = 30,
               min_rps: Optional[float] = None,
               domain: Optional[set] = None) -> pl.DataFrame:
    """基准日「门槛 -> RPS -> 排序 -> 取前 x」选股（选股器与动态重选同一实现）。

    门槛（内置不可关）：MACD 金叉 + 收盘站上均线锚 + 动量分为正 + 崩溃保护未触发
    （score 非 None 即已通过崩溃保护）。
    RPS：动量分在**当日全市场**非空分数中的分位（1=最强），min_rps ∈ [0,100] 为
    百分位下限（全市场口径，不受 domain 限定影响）。
    domain 提供时在门槛与排序前限定候选域（如指数成分/行业过滤后的命中集）。
    返回列：rank(1起) / code / score / rps；无符合项返回空表。

    无后视镜：as_of_day 的行只含截至当日收盘的信息，调用方须在次日才据此交易。
    """
    day_all = mf.feats.filter((pl.col("day") == as_of_day)
                              & pl.col("score").is_not_null())
    if day_all.height == 0:
        return _empty_pick()
    n = day_all.height
    day_all = day_all.with_columns(
        (1.0 - (pl.col("score").rank(descending=True) - 1) / n).alias("rps"))
    d = day_all.filter(pl.col("macd_ok") & pl.col("above") & (pl.col("score") > 0))
    if min_rps is not None:
        d = d.filter(pl.col("rps") >= float(min_rps) / 100.0)
    if domain is not None:
        d = d.filter(pl.col("code").is_in(list(domain)))
    if d.height == 0:
        return _empty_pick()
    return (d.sort(["score", "code"], descending=[True, False])
             .head(max(1, int(top_x)))
             .with_row_index("rank", offset=1)
             .select(["rank", "code", "score", "rps"]))
