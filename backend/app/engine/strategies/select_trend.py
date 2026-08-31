# -*- coding: utf-8 -*-
"""动态加速启动选股策略（原型·日线）

设计（P1 动态选股的原型）：
- 候选池 = config.universe（可放大到数百只）；引擎在"持仓 < max_holdings"时才允许开新仓，
  因此只要候选池里有票触发条件，就会自动补入——天然实现"持仓不足则动态选股"。
- 三个核心条件（"加速段启动"）：
  ① 金叉：MACD DIF > DEA（金叉状态）
  ② 突破：收盘价创前 breakout_n 日新高
  ③ 相对强度：RPS(rps_n) 处于横截面前 rps_top 分位
  满足 ≥ entry_need 个（默认 2）且条件首次成立时发开仓信号（避免持仓中重复加仓刷单）。
- 退出：收盘跌破慢线 ma_slow 清仓；止损由引擎风控（ATR 止损等）负责。
- 点时一致性：所有特征用当日收盘已知数据，信号当日 bar 生成、次日开盘成交（引擎语义），
  与 A1 修复口径一致，无未来函数。

信号列协议：signal / tag / reason / budget_pct。
"""
import polars as pl

from ..indicators import _rolling_params, add_macd, add_ma
from .ma_cross import Strategy


class SelectTrendStrategy(Strategy):
    id = "select_trend"
    name = "动态加速启动选股"
    description = ("全市场条件选股原型：金叉+新高突破+相对强度(满足≥entry_need个)触发加速段启动，"
                   "跌破慢线清仓；持仓不足由引擎按 max_holdings 自动补入。日线周期，建议引擎开启预热。")
    periods = ["daily"]

    @property
    def warmup_days(self) -> int:
        longest = max((int(p["default"]) for p in self.param_schema
                       if p["key"] in ("ma_slow", "breakout_n", "rps_n", "macd_slow")),
                      default=26)
        return longest + 60

    param_schema = [
        {"key": "entry_need", "label": "最少满足条件数", "type": "int", "default": 2,
         "min": 1, "max": 3, "group": "核心开关",
         "description": "金叉/突破/相对强度中至少满足几个才触发开仓"},
        {"key": "macd_fast", "label": "MACD快线", "type": "int", "default": 12, "min": 5, "max": 30,
         "group": "趋势判据"},
        {"key": "macd_slow", "label": "MACD慢线", "type": "int", "default": 26, "min": 10, "max": 60,
         "group": "趋势判据"},
        {"key": "macd_signal", "label": "MACD信号线", "type": "int", "default": 9, "min": 3, "max": 20,
         "group": "趋势判据"},
        {"key": "ma_slow", "label": "趋势慢线(退出)", "type": "int", "default": 20, "min": 5, "max": 60,
         "group": "趋势判据", "description": "收盘跌破该均线清仓"},
        {"key": "breakout_n", "label": "新高突破窗口", "type": "int", "default": 20,
         "min": 5, "max": 60, "unit": "日", "group": "选股排序",
         "description": "收盘价创前 N 日新高才计入突破条件"},
        {"key": "rps_n", "label": "相对强度窗口", "type": "int", "default": 20,
         "min": 5, "max": 60, "unit": "日", "group": "选股排序"},
        {"key": "rps_top", "label": "相对强度分位", "type": "float", "default": 0.5,
         "min": 0.1, "max": 1.0, "step": 0.05, "group": "选股排序",
         "description": "RPS 需处于横截面前 rps_top 分位才算强（0.5=前50%）"},
        {"key": "base_pct", "label": "开仓资金占比", "type": "float", "default": 30,
         "min": 5, "max": 90, "step": 1, "unit": "%", "group": "仓位"},
        {"key": "max_adds", "label": "最大加仓次数", "type": "int", "default": 1, "min": 0, "max": 4,
         "group": "仓位"},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        p = {k["key"]: k["default"] for k in self.param_schema}
        p.update({k: v for k, v in (params or {}).items() if v is not None})
        n = {k: int(p[k]) for k in ("macd_fast", "macd_slow", "macd_signal",
                                    "ma_slow", "breakout_n", "rps_n")}
        need = int(p["entry_need"])
        rps_top = float(p["rps_top"])
        base_pct = float(p["base_pct"])

        # 1) 每股特征
        feats: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = add_macd(df, n["macd_fast"], n["macd_slow"], n["macd_signal"])
            df = add_ma(df, n["ma_slow"], name="ma_slow")
            # 突破：close > 前 breakout_n 日最高（不含当日，当日收盘后可知）
            df = df.with_columns(
                pl.col("close").shift(1).rolling_max(n["breakout_n"],
                                                     **_rolling_params(n["breakout_n"]))
                .alias("prior_high"))
            # RPS：rps_n 日涨幅
            df = df.with_columns(
                (pl.col("close") / pl.col("close").shift(n["rps_n"]) - 1).alias("rps"))
            df = df.with_columns([
                (pl.col("dif") > pl.col("dea")).fill_null(False).alias("golden"),
                (pl.col("close") > pl.col("prior_high")).fill_null(False).alias("brk"),
            ])
            feats[code] = df

        # 2) 横截面 RPS 分位（逐日，仅统计当日有数据的候选）
        parts = [f.select([pl.lit(code).alias("code"),
                           pl.col("date").str.slice(0, 10).alias("day"), pl.col("rps")])
                 for code, f in feats.items()]
        rps_all = pl.concat(parts)
        thr = (rps_all.filter(pl.col("rps").is_not_null())
               .group_by("day").agg(pl.col("rps").quantile(1 - rps_top).alias("rps_thr")))
        rps_ok = (rps_all.join(thr, on="day", how="left")
                  .with_columns((pl.col("rps") >= pl.col("rps_thr")).fill_null(False)
                                .alias("rps_ok"))
                  .select(["code", "day", "rps_ok"]))

        # 3) 逐股信号
        out: dict[str, pl.DataFrame] = {}
        for code, df in feats.items():
            rok = rps_ok.filter(pl.col("code") == code).select(["day", "rps_ok"])
            df = df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
            df = df.join(rok, on="day", how="left")
            conds = (pl.col("golden").cast(pl.Int32) + pl.col("brk").cast(pl.Int32)
                     + pl.col("rps_ok").fill_null(False).cast(pl.Int32))
            entry = conds >= need
            entry_event = entry & (~entry.shift(1).fill_null(False))
            exit_sig = pl.col("close") < pl.col("ma_slow")
            df = df.with_columns([
                pl.when(entry_event).then(1)
                  .when(exit_sig).then(-1)
                  .otherwise(0).cast(pl.Int32).alias("signal"),
                pl.when(entry_event).then(pl.lit("加速启动(金叉+突破+相对强度)"))
                  .when(exit_sig).then(pl.lit("跌破慢线清仓"))
                  .otherwise(pl.lit("")).alias("reason"),
                pl.when(entry_event).then(pl.lit("开仓"))
                  .otherwise(pl.lit("清仓")).alias("tag"),
                pl.when(entry_event).then(pl.lit(base_pct))
                  .otherwise(pl.lit(None)).alias("budget_pct"),
            ])
            out[code] = df.drop("day")
        return out
