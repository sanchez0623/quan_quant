# -*- coding: utf-8 -*-
"""polars 技术指标：MA / EMA / MACD / ATR / BOLL"""
import inspect

import polars as pl


def _rolling_params(n: int) -> dict:
    """兼容 polars 新旧版本的 rolling_* 参数名（min_periods → min_samples）"""
    sig = inspect.signature(pl.Expr.rolling_mean)
    return {"min_samples": n} if "min_samples" in sig.parameters else {"min_periods": n}


def add_ma(df: pl.DataFrame, n: int, col: str = "close", name: str | None = None) -> pl.DataFrame:
    name = name or f"ma{n}"
    return df.with_columns(pl.col(col).rolling_mean(n, **_rolling_params(n)).alias(name))


def add_ema(df: pl.DataFrame, n: int, col: str = "close", name: str | None = None) -> pl.DataFrame:
    name = name or f"ema{n}"
    return df.with_columns(pl.col(col).ewm_mean(span=n, adjust=False, ignore_nulls=True).alias(name))


def add_macd(df: pl.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9,
             col: str = "close") -> pl.DataFrame:
    """返回列: dif / dea / macd_hist"""
    ef = pl.col(col).ewm_mean(span=fast, adjust=False, ignore_nulls=True)
    es = pl.col(col).ewm_mean(span=slow, adjust=False, ignore_nulls=True)
    df = df.with_columns((ef - es).alias("dif"))
    df = df.with_columns(
        pl.col("dif").ewm_mean(span=signal, adjust=False, ignore_nulls=True).alias("dea"))
    return df.with_columns(((pl.col("dif") - pl.col("dea")) * 2).alias("macd_hist"))


def add_atr(df: pl.DataFrame, n: int = 14, name: str | None = None) -> pl.DataFrame:
    """真实波幅均值 ATR；需要 high/low/close 列"""
    name = name or f"atr{n}"
    hl = pl.col("high") - pl.col("low")
    hc = (pl.col("high") - pl.col("close").shift(1)).abs()
    lc = (pl.col("low") - pl.col("close").shift(1)).abs()
    tr = pl.max_horizontal([hl, hc, lc]).fill_null(hl)
    return df.with_columns(tr.rolling_mean(n, min_samples=1).alias(name))


def add_boll(df: pl.DataFrame, n: int = 20, k: float = 2.0, col: str = "close") -> pl.DataFrame:
    mid = pl.col(col).rolling_mean(n, **_rolling_params(n))
    std = pl.col(col).rolling_std(n, **_rolling_params(n))
    return df.with_columns([
        mid.alias("boll_mid"),
        (mid + k * std).alias("boll_up"),
        (mid - k * std).alias("boll_low"),
    ])
