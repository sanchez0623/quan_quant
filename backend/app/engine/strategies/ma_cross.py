# -*- coding: utf-8 -*-
"""双均线策略：快线上穿慢线买入，下穿卖出（日线 + 5分钟）"""
from abc import ABC, abstractmethod

import polars as pl

from ..indicators import add_ma


class Strategy(ABC):
    id: str = ""
    name: str = ""
    description: str = ""
    periods: list[str] = []
    param_schema: list[dict] = []
    warmup_days: int = 0  # 指标预热建议值（交易日数），引擎据此前推数据加载窗口

    @abstractmethod
    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        """向量化计算指标与信号列，返回带 signal(1/-1/0)、reason 及附加列的每个code的df

        start_date: 回测起始日（预热期之后）。有内部状态的策略（如做T状态机）
        应在 start_date 之前只计算指标、不推进交易状态机，避免预热期"虚拟建仓"。
        """


class MaCrossStrategy(Strategy):
    id = "ma_cross"
    name = "双均线策略"
    description = "快线上穿慢线买入，下穿卖出"
    periods = ["daily", "minute5"]
    param_schema = [
        {"key": "fast", "label": "快线周期", "type": "int", "default": 5, "min": 2, "max": 60,
         "group": "均线判据", "description": "快线上穿慢线买入、下穿卖出"},
        {"key": "slow", "label": "慢线周期", "type": "int", "default": 20, "min": 5, "max": 250,
         "group": "均线判据", "description": "需明显大于快线，否则频繁交叉刷单"},
        {"key": "max_adds", "label": "最大加仓次数", "type": "int", "default": 2, "min": 0, "max": 10,
         "group": "仓位"},
        {"key": "stop_loss_pct", "label": "止损比例", "type": "float", "default": 12.0,
         "min": 1, "max": 50, "step": 0.5, "unit": "%", "group": "仓位",
         "description": "未显式设置风控时会同步到风控固定止损"},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        fast = int(params.get("fast") or 5)
        slow = int(params.get("slow") or 20)
        out: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = add_ma(df, fast, name="ma_fast")
            df = add_ma(df, slow, name="ma_slow")
            f, s = pl.col("ma_fast"), pl.col("ma_slow")
            cross_up = (f > s) & (f.shift(1) <= s.shift(1))
            cross_dn = (f < s) & (f.shift(1) >= s.shift(1))
            valid = f.is_not_null() & s.is_not_null() & f.shift(1).is_not_null() & s.shift(1).is_not_null()
            df = df.with_columns([
                pl.when(cross_up & valid).then(1)
                  .when(cross_dn & valid).then(-1)
                  .otherwise(0).cast(pl.Int32).alias("signal"),
                pl.when(cross_up & valid).then(pl.lit(f"MA{fast}上穿MA{slow}"))
                  .when(cross_dn & valid).then(pl.lit(f"MA{fast}下穿MA{slow}"))
                  .otherwise(pl.lit("")).alias("reason"),
                pl.lit("开仓").alias("tag"),
            ])
            out[code] = df
        return out
