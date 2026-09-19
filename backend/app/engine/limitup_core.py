# -*- coding: utf-8 -*-
"""涨停/连板/炸板/市场情绪基础设施（龙头低吸策略共用特征层）。

口径（全部基于不复权原始价；引擎喂入的后复权 OHLC 由调用方先除回 adj_factor）：
- 涨停价 limit_price = round(prev_close × (1+limit_pct), 2)，比例复用 Broker.limit_pct
  （主板10/创业板20/科创板20/北交所30/ST5；创业板 2020-08-24 前的 10% 阶段未建模，
  回测窗口 2024+ 无影响）
- 收盘涨停 is_limit_close：close 达到涨停价（一字板天然满足）
- 盘中触板 touched_limit：high 达到涨停价（炸板前提）
- 炸板 limit_broken：盘中触板但收盘未封住
- 一字板 one_word：high==low 且收在涨停价（全天封死无换手）
- 连板数 consec_boards：连续收盘涨停天数（未涨停归零）
- 市场情绪 market_sentiment：逐日聚合 涨停家数/触板家数/炸板家数/炸板率/
  最高连板高度/连板晋级率/昨涨停今溢价

所有特征只依赖当日及更早数据（收盘后可知），配合引擎
「T 日收盘判定信号 → T+1 开盘成交」契约天然无未来函数。
"""
import polars as pl

from .broker import Broker
from .indicators import _rolling_params

# 涨停判定价格容差（与 broker.is_limit_up 一致，吸收复权往返浮点误差）
_LIMIT_TOL = 0.011


def limit_price(prev_close: float, code: str, is_st: bool = False) -> float | None:
    """涨停价（前收盘 × (1+板块涨跌幅) 四舍五入到分）"""
    if prev_close is None or prev_close <= 0:
        return None
    pct = Broker.limit_pct(code, is_st=is_st)
    return round(prev_close * (1 + pct) + 1e-9, 2)


def daily_flags(df: pl.DataFrame, code: str, is_st: bool = False) -> pl.DataFrame:
    """单票日线（原始价 OHLC，按日期升序）→ 追加涨停/连板特征列。

    输入需含列：date,open,high,low,close,volume,amount。
    追加列：limit_price/is_limit_close/touched_limit/limit_broken/one_word/
    consec_boards/is_limit_close_prev。
    """
    pct = Broker.limit_pct(code, is_st=is_st)
    df = df.sort("date").with_columns(pl.col("close").shift(1).alias("prev_close"))
    df = df.with_columns(
        ((pl.col("prev_close") * (1 + pct) + 1e-9).round(2)).alias("limit_price"))
    df = df.with_columns([
        ((pl.col("close") - pl.col("limit_price")).abs() <= _LIMIT_TOL)
        .fill_null(False).alias("is_limit_close"),
        (pl.col("high") >= pl.col("limit_price") - _LIMIT_TOL)
        .fill_null(False).alias("touched_limit"),
    ])
    df = df.with_columns(
        (pl.col("touched_limit") & ~pl.col("is_limit_close")).alias("limit_broken"))
    df = df.with_columns(
        ((pl.col("high") == pl.col("low"))
         & pl.col("is_limit_close")).alias("one_word"))
    # 连板数：连续收盘涨停的游程长度（游程分组技巧：False 处递增的分组 id）
    df = df.with_columns(
        (~pl.col("is_limit_close")).cast(pl.Int32).cum_sum().alias("_run"))
    df = df.with_columns(
        pl.when(pl.col("is_limit_close"))
        .then(pl.col("is_limit_close").cast(pl.Int32).cum_sum().over("_run"))
        .otherwise(0).cast(pl.Int32).alias("consec_boards")).drop("_run")
    df = df.with_columns(
        pl.col("is_limit_close").shift(1).fill_null(False).alias("is_limit_close_prev"))
    return df


def market_sentiment(flags: list[pl.DataFrame]) -> pl.DataFrame:
    """逐码涨停特征表 → 逐日市场情绪表（date 升序）。

    列：n_limit(收盘涨停家数)/n_touched(触板家数)/n_broken(炸板家数)/
    broken_rate(炸板率=炸板/触板)/max_boards(最高连板)/promote_rate(连板晋级率=
    昨日涨停股今日再涨停比例)/limit_premium(昨涨停股今日涨跌幅均值)。
    """
    parts = []
    for f in flags:
        f = f.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1).alias("_pct_chg"))
        parts.append(f.select([
            "date", "is_limit_close", "touched_limit", "limit_broken",
            "consec_boards", "_pct_chg",
            pl.col("is_limit_close").shift(1).fill_null(False).alias("_limit_prev"),
        ]))
    allf = pl.concat(parts)
    agg = allf.group_by("date").agg([
        pl.col("is_limit_close").sum().alias("n_limit"),
        pl.col("touched_limit").sum().alias("n_touched"),
        pl.col("limit_broken").sum().alias("n_broken"),
        pl.col("consec_boards").max().alias("max_boards"),
        pl.col("_limit_prev").sum().alias("_promo_den"),
        (pl.col("is_limit_close") & pl.col("_limit_prev")).sum().alias("_promo_num"),
        pl.when(pl.col("_limit_prev")).then(pl.col("_pct_chg")).otherwise(0.0)
          .sum().alias("_prem_sum"),
    ])
    return agg.with_columns([
        pl.when(pl.col("n_touched") > 0)
          .then(pl.col("n_broken") / pl.col("n_touched")).otherwise(0.0)
          .alias("broken_rate"),
        pl.when(pl.col("_promo_den") > 0)
          .then(pl.col("_promo_num") / pl.col("_promo_den")).otherwise(None)
          .alias("promote_rate"),
        pl.when(pl.col("_promo_den") > 0)
          .then(pl.col("_prem_sum") / pl.col("_promo_den")).otherwise(None)
          .alias("limit_premium"),
    ]).with_columns(
        pl.col("max_boards").cast(pl.Int32)).sort("date").drop(
        ["_promo_den", "_promo_num", "_prem_sum"])


def regime_gate(sent: pl.DataFrame, broken_rate_th: float, n_limit_floor: int,
                regime_ma_n: int, euphoria_boards: int) -> pl.DataFrame:
    """市场情绪表 → 情绪周期门控列（date/gate_off/euphoria）。

    - 退潮：炸板率 > broken_rate_th 且最高连板低于其 regime_ma_n 日均值（高度回落）
    - 冰点：涨停家数 < n_limit_floor
    - gate_off = 退潮 | 冰点（停开新仓）
    - euphoria：最高连板 >= euphoria_boards（情绪高潮，仓位缩放）
    """
    return sent.select([
        "date",
        ((((pl.col("broken_rate") > broken_rate_th)
           & (pl.col("max_boards")
              < pl.col("max_boards").rolling_mean(
                  regime_ma_n, **_rolling_params(regime_ma_n))))
          | (pl.col("n_limit") < n_limit_floor))
         .fill_null(False).alias("gate_off")),
        (pl.col("max_boards") >= euphoria_boards).fill_null(False).alias("euphoria"),
    ])
