# -*- coding: utf-8 -*-
"""网格做T策略：底仓 + ATR 自适应网格（动态阈值 = 近N日ATR/close 的倍数）
价格跌破下网格线买回、升破上网格线卖出部分底仓，体现"卖旧买新"做T记录。
适合 5 分钟周期（日线亦可运行但无法日内做T）。
"""
import polars as pl

from ..indicators import add_atr
from .ma_cross import Strategy


class GridTStrategy(Strategy):
    id = "grid_t"
    name = "网格做T策略"
    description = "底仓+ATR自适应网格做T（适合5分钟周期；动态阈值=近N日ATR/close的倍数）"
    periods = ["minute5", "daily"]
    param_schema = [
        {"key": "base_pct", "label": "底仓资金占比", "type": "float", "default": 30,
         "min": 5, "max": 90, "step": 1, "unit": "%"},
        {"key": "grid_atr_mult", "label": "网格ATR倍数", "type": "float", "default": 1.5,
         "min": 0.2, "max": 8, "step": 0.1},
        {"key": "atr_period", "label": "ATR周期", "type": "int", "default": 14, "min": 3, "max": 60},
        {"key": "max_t_times", "label": "日内T次数上限", "type": "int", "default": 4, "min": 1, "max": 20},
        {"key": "max_adds", "label": "最大加仓次数", "type": "int", "default": 0, "min": 0, "max": 10},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        base_pct = float(params.get("base_pct") or 30)
        mult = float(params.get("grid_atr_mult") or 1.5)
        atr_n = int(params.get("atr_period") or 14)
        max_t = int(params.get("max_t_times") or 4)
        # T_REFACTOR：t_mode=off 关闭做T（C 基线）；time 模式对网格策略回退为普通网格
        if str(params.get("t_mode") or "grid") == "off":
            max_t = 0

        out: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = self._with_day_atr_pct(df, atr_n)
            signals, tags, reasons = self._walk(df, base_pct, mult, max_t, start_date)
            df = df.with_columns([
                pl.Series("signal", signals, dtype=pl.Int32),
                pl.Series("tag", tags, dtype=pl.Utf8),
                pl.Series("reason", reasons, dtype=pl.Utf8),
            ])
            out[code] = df
        return out

    # ---------------- 内部 ----------------

    @staticmethod
    def _with_day_atr_pct(df: pl.DataFrame, atr_n: int) -> pl.DataFrame:
        """按交易日聚合出日线，计算 ATR(N)/close 百分比并映射回每根bar"""
        day_col = pl.col("date").str.slice(0, 10).alias("day")
        daily = (df.with_columns(day_col)
                   .group_by("day").agg(pl.col("close").last().alias("close"),
                                        pl.col("open").first().alias("open"),
                                        pl.col("high").max().alias("high"),
                                        pl.col("low").min().alias("low"))
                   .sort("day"))
        daily = add_atr(daily, atr_n, name="d_atr")
        daily = daily.with_columns(
            (pl.col("d_atr") / pl.col("close")).alias("day_atr_pct"))
        # 前 N 日 ATR 未就绪时用滚动可用值（min_samples=1 已保证）
        return (df.with_columns(day_col)
                .join(daily.select(["day", "day_atr_pct"]), on="day", how="left"))

    @staticmethod
    def _walk(df: pl.DataFrame, base_pct: float, mult: float,
              max_t: int, start_date: str | None = None) -> tuple[list[int], list[str], list[str]]:
        """逐bar网格状态机：返回 (signal, tag, reason) 列表
        start_date 之前为预热期：只跳过不推进状态机（避免虚拟建仓）"""
        n = df.height
        signals = [0] * n
        tags = [""] * n
        reasons = [""] * n
        rows = df.select(["date", "close", "day_atr_pct"]).iter_rows()
        opened = False
        cur_day = None
        ref = None
        t_count = 0
        for i, (date, close, atr_pct) in enumerate(rows):
            day = date[:10]
            if start_date and day < start_date:
                continue  # 预热期：不产生信号
            if day != cur_day:  # 新交易日
                cur_day = day
                ref = None
                t_count = 0
            if not opened:
                # 底仓建仓信号（首日首bar）
                signals[i] = 1
                tags[i] = "开仓"
                reasons[i] = f"建立底仓({base_pct:g}%资金)"
                opened = True
                ref = close
                continue
            if ref is None:
                ref = close
                continue
            if atr_pct is None or t_count >= max_t:
                continue
            g = float(atr_pct) * mult  # 动态网格阈值
            if g <= 0:
                continue
            if close <= ref * (1 - g):
                signals[i] = 1
                tags[i] = "做T"
                reasons[i] = f"跌破下网格线(阈值{g * 100:.2f}%)买回"
                ref = close
                t_count += 1
            elif close >= ref * (1 + g):
                signals[i] = -1
                tags[i] = "做T"
                reasons[i] = f"升破上网格线(阈值{g * 100:.2f}%)卖出"
                ref = close
                t_count += 1
        return signals, tags, reasons
