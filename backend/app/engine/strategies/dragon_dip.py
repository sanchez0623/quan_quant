# -*- coding: utf-8 -*-
"""龙头低吸策略（dragon_dip）：买在分歧、卖在一致 —— 日线近似版。

框架映射（T 日收盘判定信号 → 引擎 T+1 开盘成交，T+1 已由引擎内建）：
- 候选池：近 board_window 日有涨停（涨停基因）、当日成交额 ≥ min_amount、
  非新股（上市满 new_stock_days 根 bar）、参照日非一字板；
  「辨识度/市场地位」以连板高度横截面排名代理（每日仅 top_n 可开仓）
- 买点（entry_type 切换，all=三合一）：
  zha 炸板低吸：T 日盘中触板未封收，自涨停价回落 [pullback_min, pullback_max]，
    量未爆量（框架：放量但不极端）
  yin 首阴低吸：T-1 连板 ≥ min_boards，T 收首阴跌幅 [yin_min, yin_max]，
    未破 MA5，放量未爆量
  gap 竞价低吸：T-1 收盘涨停，T 低开 ≥ gap_down_min 且当日收复开盘（承接确认，
    日线近似=企稳日尾盘介入语义；分钟级竞价直买留二期）
- 情绪门控：退潮（炸板率超阈 + 最高板回落）/冰点（涨停家数过低）停开仓；
  高潮（最高板 ≥ euphoria_boards）开仓预算 × euphoria_scale
- 卖出：涨停晋级金字塔减仓（pyr_step × 最多 pyr_max 次）；破 MA5 清仓；
  爆量滞涨清仓；日内/隔日止损走风控 stop_loss_pct

局限（如实声明）：龙虎榜/游资/题材热度缺失，辨识度用连板高度+成交额代理；
情绪指标与排名基于回测 universe 内聚合，建议全市场静态池运行；
打板买点无法在日线近似（封板瞬间成交不可模拟），留二期分钟级实现。
"""
import polars as pl

from ...data import store
from .. import limitup_core as lc
from ..indicators import _rolling_params
from .ma_cross import Strategy


class DragonDipStrategy(Strategy):
    id = "dragon_dip"
    name = "龙头低吸"
    description = ("买在分歧卖在一致：连板龙头炸板回落/首阴/竞价低开三合一低吸，"
                   "涨停晋级金字塔减仓，破5日线清仓，情绪周期门控。"
                   "建议全市场静态池 + daily 周期。")
    periods = ["daily"]
    warmup_days = 40

    # 分组约定与 momentum_t 一致：group 按序渲染；advanced 二次微调收起
    param_schema = [
        # ---- G1 核心开关 ----
        {"key": "entry_type", "label": "买点类型", "type": "categorical",
         "group": "核心开关",
         "choices": ["all|三合一", "zha|炸板低吸", "yin|首阴低吸", "gap|竞价低吸"],
         "default": "all",
         "description": "炸板低吸=主战；首阴/竞价=辅战；all=三者并集"},
        {"key": "top_n", "label": "每日最多开仓数", "type": "int", "default": 2,
         "min": 1, "max": 5, "group": "核心开关",
         "description": "同日多信号时按（连板高度,成交额）降序取前 top_n；"
                        "并发持仓上限建议风控 max_holdings 与此一致"},
        {"key": "base_pct", "label": "单票资金占比", "type": "float", "default": 50,
         "min": 10, "max": 90, "step": 1, "unit": "%", "group": "核心开关",
         "description": "开仓预算占组合净值比例，实际仍受风控个股/总仓上限约束"},
        {"key": "regime_gate_on", "label": "情绪门控", "type": "categorical",
         "choices": ["on|开启", "off|关闭"], "default": "on",
         "group": "核心开关",
         "description": "退潮/冰点期停开仓，高潮期仓位缩放"},
        {"key": "exclude_one_word", "label": "排除一字票", "type": "categorical",
         "choices": ["on|开启", "off|关闭"], "default": "on",
         "group": "核心开关",
         "description": "参照日一字板（无换手纯情绪票，开板即终点）不开仓"},
        # ---- G2 候选池 ----
        {"key": "board_window", "label": "涨停基因窗口", "type": "int", "default": 5,
         "min": 3, "max": 10, "unit": "日", "group": "候选池",
         "description": "近 N 个交易日出现过涨停（触板即算）才有资格"},
        {"key": "min_boards", "label": "首阴最低连板数", "type": "int", "default": 3,
         "min": 2, "max": 6, "group": "候选池",
         "description": "首阴低吸要求此前连续涨停板数"},
        {"key": "min_amount", "label": "最低成交额", "type": "float", "default": 3.0,
         "min": 0.5, "max": 50, "step": 0.5, "unit": "亿", "group": "候选池",
         "description": "流动性门槛，保证 T+1 进出"},
        {"key": "new_stock_days", "label": "新股保护期", "type": "int", "default": 6,
         "min": 0, "max": 20, "unit": "交易日", "group": "候选池",
         "description": "上市未满 N 根 bar 不参与（无涨跌幅限制期波动不可控）"},
        # ---- G3 买点·炸板 ----
        {"key": "pullback_min", "label": "炸板回落下限", "type": "float",
         "default": 3.0, "min": 0.5, "max": 10, "step": 0.5, "unit": "%",
         "group": "买点·炸板",
         "description": "自涨停价回落下限（太小无安全垫）"},
        {"key": "pullback_max", "label": "炸板回落上限", "type": "float",
         "default": 7.0, "min": 1, "max": 15, "step": 0.5, "unit": "%",
         "group": "买点·炸板",
         "description": "自涨停价回落上限（太大说明真弱）"},
        # ---- G4 买点·首阴 ----
        {"key": "yin_min", "label": "首阴跌幅下限", "type": "float", "default": 3.0,
         "min": 1, "max": 8, "step": 0.5, "unit": "%", "group": "买点·首阴",
         "description": "首阴跌幅下限（太小不算分歧）"},
        {"key": "yin_max", "label": "首阴跌幅上限", "type": "float", "default": 5.0,
         "min": 1, "max": 10, "step": 0.5, "unit": "%", "group": "买点·首阴",
         "description": "首阴跌幅上限（太大疑似出货）"},
        # ---- G5 买点·竞价低开 ----
        {"key": "gap_down_min", "label": "竞价低开阈值", "type": "float",
         "default": 5.0, "min": 2, "max": 9.8, "step": 0.5, "unit": "%",
         "group": "买点·竞价低开",
         "description": "前日涨停后次日低开幅度下限（情绪错杀黄金坑）"},
        # ---- G6 卖出·金字塔 ----
        {"key": "pyr_step", "label": "晋级减仓比例", "type": "float", "default": 30,
         "min": 10, "max": 50, "step": 5, "unit": "%", "group": "卖出·金字塔",
         "description": "持仓期每晋级一个涨停减仓比例（越涨越卖）"},
        {"key": "pyr_max", "label": "最多减仓次数", "type": "int", "default": 3,
         "min": 1, "max": 5, "group": "卖出·金字塔",
         "description": "金字塔减仓上限次数，剩余底仓博傻"},
        # ---- G7 卖出·走弱 ----
        {"key": "vol_burst_max", "label": "爆量倍数", "type": "float", "default": 3.0,
         "min": 1.5, "max": 8, "step": 0.5, "unit": "×20日均量",
         "group": "卖出·走弱", "advanced": True,
         "description": "买点侧：放量超此倍数视为爆量禁止首阴/炸板入场；"
                        "卖侧：爆量且滞涨触发清仓"},
        {"key": "stall_gain_max", "label": "滞涨涨幅上限", "type": "float",
         "default": 2.0, "min": 0, "max": 5, "step": 0.5, "unit": "%",
         "group": "卖出·走弱", "advanced": True,
         "description": "放量创20日新高但涨幅低于此值 -> 放量滞涨清仓"},
        # ---- G8 情绪门控参数 ----
        {"key": "broken_rate_th", "label": "退潮炸板率阈值", "type": "float",
         "default": 0.40, "min": 0.1, "max": 0.8, "step": 0.05,
         "group": "情绪门控", "advanced": True,
         "description": "炸板率（炸板/触板）超过此值且最高板回落 -> 退潮期停开仓"},
        {"key": "n_limit_floor", "label": "冰点涨停家数", "type": "int",
         "default": 20, "min": 0, "max": 100, "step": 5,
         "group": "情绪门控", "advanced": True,
         "description": "涨停家数低于此值 -> 冰点期停开仓"},
        {"key": "regime_ma_n", "label": "最高板均线窗口", "type": "int",
         "default": 10, "min": 5, "max": 30, "unit": "日",
         "group": "情绪门控", "advanced": True,
         "description": "最高连板高度低于其 N 日均值视为高度回落"},
        {"key": "euphoria_boards", "label": "高潮最高板数", "type": "int",
         "default": 6, "min": 3, "max": 10, "group": "情绪门控", "advanced": True,
         "description": "市场最高连板达到此值视为情绪高潮"},
        {"key": "euphoria_scale", "label": "高潮仓位系数", "type": "float",
         "default": 0.6, "min": 0.1, "max": 1.0, "step": 0.1, "unit": "×",
         "group": "情绪门控", "advanced": True,
         "description": "高潮期开仓预算乘数（框架：高潮期谨慎，3~5成）"},
        # ---- G9 风控 ----
        {"key": "stop_loss_pct", "label": "止损比例", "type": "float", "default": 5.0,
         "min": 1, "max": 20, "step": 0.5, "unit": "%", "group": "风控",
         "description": "未显式设置风控时会同步到风控固定止损"},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        p = {k["key"]: k["default"] for k in self.param_schema}
        p.update({k: v for k, v in (params or {}).items() if v is not None})

        # ST 标记（涨停价 5% 差异化；exclude_st=True 时 universe 已无 ST，兜底覆盖）
        st_map: dict[str, bool] = {}
        basic = store.read_stock_basic()
        if basic is not None and basic.height:
            st_map = {r[0]: bool(r[1]) for r in
                      basic.select(["code", "st"]).iter_rows()}

        work = {code: self._build_work(df, code, st_map.get(code, False), p)
                for code, df in data.items()}
        # 情绪指标基于 universe 内聚合（建议全市场池）；空池直接返回空信号
        if not work:
            return {}
        sent = lc.market_sentiment(list(work.values()))
        if str(p.get("regime_gate_on") or "on") == "on":
            regime = lc.regime_gate(sent, float(p["broken_rate_th"]),
                                    int(p["n_limit_floor"]), int(p["regime_ma_n"]),
                                    int(p["euphoria_boards"]))
        else:
            regime = sent.select("date").with_columns([
                pl.lit(False).alias("gate_off"),
                pl.lit(False).alias("euphoria")])
        allowed = self._rank_entries(work, regime, int(p["top_n"]))
        out: dict[str, pl.DataFrame] = {}
        for code, w in work.items():
            w = w.join(regime, on="date", how="left").with_columns([
                pl.col("gate_off").fill_null(False),
                pl.col("euphoria").fill_null(False)])
            out[code] = self._emit(w, allowed.get(code, set()), p, start_date)
        return out

    # ---------------- 特征构建（原始价涨停判定 + 复权价相对特征） ----------------

    @staticmethod
    def _build_work(df: pl.DataFrame, code: str, is_st: bool, p: dict) -> pl.DataFrame:
        """datafeed 后复权日线 -> 工作表：除回原始价算涨停，复权价算均线/涨幅"""
        adj = pl.col("adj_factor").cast(pl.Float64).fill_null(1.0)
        adj = pl.when(adj > 0).then(adj).otherwise(1.0)
        w = df.sort("date").with_columns([
            (pl.col("open") / adj).alias("_open"),
            (pl.col("high") / adj).alias("_high"),
            (pl.col("low") / adj).alias("_low"),
            (pl.col("close") / adj).alias("_close"),
        ]).with_columns(pl.col("_close").shift(1).alias("_prev_close"))
        flags = lc.daily_flags(
            w.select([pl.col("date"),
                      pl.col("_open").alias("open"), pl.col("_high").alias("high"),
                      pl.col("_low").alias("low"), pl.col("_close").alias("close"),
                      pl.col("volume"), pl.col("amount")]), code, is_st)
        feat_cols = ["limit_price", "is_limit_close", "touched_limit",
                     "limit_broken", "one_word", "consec_boards",
                     "is_limit_close_prev"]
        w = w.with_columns(flags.select(feat_cols))
        w = w.with_columns(pl.int_range(pl.len()).cast(pl.Int32).alias("_bar_no"))

        min_amt = float(p["min_amount"]) * 1e8
        pullback = ((pl.col("limit_price") - pl.col("_close"))
                    / pl.col("limit_price"))
        pct_chg = pl.col("close") / pl.col("close").shift(1) - 1
        ma5 = pl.col("close").rolling_mean(5, **_rolling_params(5))
        vol_ratio = (pl.col("volume")
                     / pl.col("volume").rolling_mean(20, **_rolling_params(5)))
        liquid = pl.col("amount") >= min_amt
        mature = pl.col("_bar_no") >= int(p["new_stock_days"])
        gene = (pl.col("touched_limit").cast(pl.Int32)
                .rolling_sum(int(p["board_window"]),
                             **_rolling_params(1)) >= 1)
        fresh = ~pl.col("one_word").shift(1).fill_null(True)
        if str(p.get("exclude_one_word") or "on") != "on":
            fresh = pl.lit(True)
        consec_prev = pl.col("consec_boards").shift(1).fill_null(0)
        burst = float(p["vol_burst_max"])

        et = str(p.get("entry_type") or "all")
        on = {"zha", "yin", "gap"} if et == "all" else {et}
        if "zha" in on:
            entry_zha = (pl.col("touched_limit") & ~pl.col("is_limit_close")
                         & pullback.ge(float(p["pullback_min"]) / 100)
                         & pullback.le(float(p["pullback_max"]) / 100)
                         & (vol_ratio <= burst) & liquid & mature & gene
                         & ~pl.col("one_word"))
        else:
            entry_zha = pl.lit(False)
        if "yin" in on:
            entry_yin = ((consec_prev >= int(p["min_boards"]))
                         & (pl.col("close") < pl.col("open"))
                         & pct_chg.ge(-float(p["yin_max"]) / 100)
                         & pct_chg.le(-float(p["yin_min"]) / 100)
                         & (pl.col("close") >= ma5)
                         & (vol_ratio >= 1.0) & (vol_ratio <= burst)
                         & liquid & mature & gene & fresh)
        else:
            entry_yin = pl.lit(False)
        if "gap" in on:
            entry_gap = (pl.col("is_limit_close_prev")
                         & (pl.col("_open")
                            <= pl.col("_prev_close") * (1 - float(p["gap_down_min"]) / 100))
                         & (pl.col("close") > pl.col("open"))
                         & liquid & mature & fresh)
        else:
            entry_gap = pl.lit(False)

        w = w.with_columns([
            consec_prev.cast(pl.Int32).alias("_consec_prev"),
            ma5.alias("_ma5"),
            ((vol_ratio >= burst) & (pct_chg <= float(p["stall_gain_max"]) / 100))
            .fill_null(False).alias("_vol_stall"),
        ]).with_columns([
            entry_zha.fill_null(False).alias("_e_zha"),
            entry_yin.fill_null(False).alias("_e_yin"),
            entry_gap.fill_null(False).alias("_e_gap"),
        ]).with_columns(
            (pl.col("_e_zha") | pl.col("_e_yin") | pl.col("_e_gap")).alias("_entry"))
        return w

    # ---------------- 排名：每日按（连板高度,成交额）取 top_n ----------------

    @staticmethod
    def _rank_entries(work: dict[str, pl.DataFrame], regime: pl.DataFrame,
                      top_n: int) -> dict[str, set]:
        """T 日收盘可见信息排名（决策在 T 收盘、执行 T+1 开盘，无未来函数）；
        门控日（退潮/冰点）不产生开仓资格"""
        gate = {r[0]: bool(r[1]) for r in regime.select(["date", "gate_off"]).iter_rows()}
        by_day: dict[str, list] = {}
        for code, w in work.items():
            for day, ea, cb, amt in w.select(
                    ["date", "_entry", "_consec_prev", "amount"]).iter_rows():
                if ea and not gate.get(day, False):
                    by_day.setdefault(day, []).append(
                        (int(cb or 0), float(amt or 0), code))
        out: dict[str, set] = {}
        for day, items in by_day.items():
            items.sort(key=lambda x: (-x[0], -x[1], x[2]))
            for _cb, _amt, code in items[:max(1, top_n)]:
                out.setdefault(code, set()).add(day)
        return out

    # ---------------- 状态机：开仓 / 金字塔减仓 / 走弱清仓 ----------------

    @staticmethod
    def _emit(w: pl.DataFrame, allowed_days: set, p: dict,
              start_date: str | None) -> pl.DataFrame:
        n = w.height
        signals = [0] * n
        tags = [""] * n
        reasons = [""] * n
        budgets: list[float | None] = [None] * n
        t_ratios: list[float | None] = [None] * n
        reduces: list[float | None] = [None] * n

        base_pct = float(p["base_pct"])
        pyr_step = float(p["pyr_step"])
        pyr_max = int(p["pyr_max"])
        eup_scale = float(p["euphoria_scale"])
        holding = False
        reduces_done = 0

        cols = ["date", "close", "_ma5", "_vol_stall", "is_limit_close",
                "_entry", "_e_zha", "_e_yin", "_e_gap", "gate_off", "euphoria"]
        for i, row in enumerate(w.select(cols).iter_rows()):
            (date, close, ma5, vol_stall, is_lc, entry, e_zha, e_yin, e_gap,
             gate_off, euphoria) = row
            day = date[:10]
            if start_date and day < start_date:
                continue  # 预热期：不推进状态机
            if not holding:
                if entry and day in allowed_days and not gate_off:
                    if e_zha:
                        et = "炸板低吸"
                    elif e_yin:
                        et = "首阴低吸"
                    else:
                        et = "竞价低吸"
                    budgets[i] = base_pct * (eup_scale if euphoria else 1.0)
                    reasons[i] = f"{et}，买在分歧"
                    signals[i] = 1
                    tags[i] = "开仓"
                    holding = True
                    reduces_done = 0
                continue
            # ---- 持仓中：走弱清仓（最高优先级） ----
            if (ma5 is not None and close < ma5) or vol_stall:
                signals[i] = -1
                tags[i] = ""
                reasons[i] = ("跌破5日线清仓" if ma5 is not None and close < ma5
                              else "爆量滞涨清仓")
                holding = False
                continue
            # ---- 涨停晋级金字塔减仓 ----
            if is_lc and reduces_done < pyr_max:
                signals[i] = -1
                tags[i] = "减仓"
                reduces[i] = pyr_step
                reasons[i] = f"涨停晋级金字塔减仓（{reduces_done + 1}/{pyr_max}）"
                reduces_done += 1
                continue

        return w.with_columns([
            pl.Series("signal", signals, dtype=pl.Int32),
            pl.Series("tag", tags, dtype=pl.Utf8),
            pl.Series("reason", reasons, dtype=pl.Utf8),
            pl.Series("budget_pct", budgets, dtype=pl.Float64),
            pl.Series("t_ratio", t_ratios, dtype=pl.Float64),
            pl.Series("reduce_pct", reduces, dtype=pl.Float64),
        ])
