# -*- coding: utf-8 -*-
"""动量趋势+做T策略：动量趋势为主，做T为增强。

架构：
- 趋势层（日线级）：MACD+慢线+斜率三重确认建仓（信号强度决定底仓 10%~70% 动态仓位）；
  持仓中金字塔加仓（突破N日新高、规模递减、冷却期）；乖离过热减仓；MACD死叉+跌破慢线双确认清仓。
- 做T层（分钟级）：ATR 自适应网格，阈值 = ATR%/close × 倍数（带费用下限）；
  波动状态（ATR% 相对滚动分位数定档，每只票自适应）连续调整网格宽度与T比例；趋势乖离非对称（强趋势放宽卖出阈值防卖飞）。
- 选股层（横截面）：universe 内按动量分排名，仅 top_n 可建仓（可少于 top_n）。

信号列协议：signal / tag / reason / budget_pct（开仓·加仓预算%）/
t_ratio（做T比例%）/ reduce_pct（减仓比例%）。
"""
import polars as pl

from .. import momentum_core as mc
from .ma_cross import Strategy


class MomentumTStrategy(Strategy):
    id = "momentum_t"
    name = "动量趋势+做T"
    description = ("动量三重确认建仓(底仓10~70%动态)+金字塔加仓+过热减仓+双确认清仓；"
                   "ATR自适应非对称网格做T(动态T比例)。适合5分钟周期，建议开启引擎预热。")
    periods = ["minute5", "daily"]  # daily 为 E 格（纯日线趋势层稳健性参考）

    @property
    def warmup_days(self) -> int:
        """指标预热建议值（交易日数）= 最长回看参数默认值 + 缓冲。

        只影响数据加载量，不影响正确性。缓冲 180 ≈ 其余主要回看窗口默认值之和
        （trend_ma 60 + vol_window 120），保证极端参数下滚动/EMA 指标完全预热。
        """
        lookback = {"trend_ma", "vol_window", "crash_vol_n", "mom_long",
                    "add_breakout_n", "slope_n"}
        longest = max((int(p["default"]) for p in self.param_schema
                       if p["key"] in lookback and p.get("default")), default=0)
        return longest + 180

    # 分组约定（供前端按"对回测结果的影响维度"折叠展示）：
    #   group     = 分组名，按下方出现顺序渲染
    #   advanced  = 二次微调项，组内默认收起
    #   show_if   = {依赖key: 允许值列表}，依赖值未设置时不隐藏（避免首帧闪烁）
    param_schema = [
        # ---- G1 核心开关：决定跑的是哪一类实验，改动影响最大 ----
        {"key": "t_mode", "label": "做T机制", "type": "categorical", "group": "核心开关",
         "choices": ["grid", "discipline", "time", "off"], "default": "grid",
         "description": "grid=网格+双止损(L1)；discipline=回补纪律(L2)；time=时点规律T(D)；"
                        "off=关闭做T(C)。选 off/time 时下方网格类参数自动隐藏"},
        {"key": "max_t_times", "label": "日内T次数上限", "type": "int", "default": 4, "min": 0, "max": 10,
         "group": "核心开关", "show_if": {"t_mode": ["grid", "discipline", "time"]},
         "description": "每日最多做T几次；0=关闭做T层（对比实验C/D格）。受风控 max_intraday_trades 二次约束"},
        {"key": "trend_clock", "label": "趋势时钟", "type": "categorical", "group": "核心开关",
         "choices": ["intraday", "daily"], "default": "intraday",
         "description": "intraday=盘中触发；daily=趋势信号仅在当日末bar评估、次日开盘成交（做T不受限）"},
        {"key": "top_n", "label": "最大持仓只数", "type": "int", "default": 3, "min": 1, "max": 10,
         "group": "核心开关", "description": "universe 内按动量分排名，仅前 top_n 可建仓（可少于该数）"},
        # ---- G2 趋势判据：决定何时进/出，三重确认建仓 + 双确认清仓 ----
        {"key": "macd_fast", "label": "MACD快线", "type": "int", "default": 12, "min": 5, "max": 30,
         "group": "趋势判据"},
        {"key": "macd_slow", "label": "MACD慢线", "type": "int", "default": 26, "min": 10, "max": 60,
         "group": "趋势判据"},
        {"key": "macd_signal", "label": "MACD信号线", "type": "int", "default": 9, "min": 3, "max": 20,
         "group": "趋势判据"},
        {"key": "trend_ma", "label": "趋势慢线周期", "type": "int", "default": 60, "min": 20, "max": 120,
         "group": "趋势判据", "description": "站上/跌破该均线是建仓与清仓的硬条件"},
        {"key": "slope_n", "label": "斜率确认窗口", "type": "int", "default": 5, "min": 2, "max": 10,
         "group": "趋势判据", "description": "均线斜率向上才算三重确认（试仓升级满配的触发条件）"},
        # ---- G3 选股排序：决定买谁（多周期风险调整动量）----
        {"key": "mom_short", "label": "短周期动量", "type": "int", "default": 20, "min": 5, "max": 40,
         "unit": "日", "group": "选股排序"},
        {"key": "mom_mid", "label": "中周期动量", "type": "int", "default": 60, "min": 30, "max": 90,
         "unit": "日", "group": "选股排序"},
        {"key": "mom_long", "label": "长周期动量", "type": "int", "default": 120, "min": 90, "max": 200,
         "unit": "日", "group": "选股排序"},
        {"key": "w_short", "label": "短周期权重", "type": "float", "default": 0.5, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序"},
        {"key": "w_mid", "label": "中周期权重", "type": "float", "default": 0.3, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序", "description": "长周期权重 = 1 - 短 - 中"},
        # ---- G4 建仓与加仓：决定仓位曲线 ----
        {"key": "base_pct_min", "label": "试仓资金占比", "type": "float", "default": 10,
         "min": 5, "max": 40, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "仅金叉+站上慢线（未确认斜率）时的首仓比例"},
        {"key": "base_pct_max", "label": "满配资金占比", "type": "float", "default": 50,
         "min": 30, "max": 90, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "三重确认后的目标仓位；实际仍受风控个股上限约束"},
        {"key": "max_adds", "label": "最大加仓次数", "type": "int", "default": 2, "min": 0, "max": 4,
         "group": "建仓与加仓"},
        {"key": "add_scale", "label": "加仓规模递减系数", "type": "float", "default": 0.5,
         "min": 0.2, "max": 0.8, "step": 0.1, "group": "建仓与加仓",
         "description": "第 n 次加仓预算 = 满配 × 系数^n（金字塔越加越小）"},
        {"key": "add_cooldown", "label": "加仓冷却期", "type": "int", "default": 5, "min": 1, "max": 20,
         "unit": "交易日", "group": "建仓与加仓"},
        {"key": "add_breakout_n", "label": "新高突破窗口", "type": "int", "default": 20, "min": 5, "max": 60,
         "unit": "日", "group": "建仓与加仓", "description": "创 N 日新高才允许金字塔加仓"},
        # ---- G5 过热减仓：决定何时主动锁盈 ----
        {"key": "overheat_k", "label": "过热乖离倍数", "type": "float", "default": 3.0,
         "min": 1, "max": 6, "step": 0.5, "unit": "×ATR", "group": "过热减仓",
         "description": "价格高于慢线 N 倍 ATR 视为过热，触发减仓"},
        {"key": "reduce_pct", "label": "过热减仓比例", "type": "float", "default": 33,
         "min": 10, "max": 50, "step": 1, "unit": "%", "group": "过热减仓"},
        {"key": "reduce_cooldown", "label": "减仓冷却期", "type": "int", "default": 10, "min": 1, "max": 30,
         "unit": "交易日", "group": "过热减仓"},
        # ---- G6 做T·网格：T 收益的主要来源（grid/discipline 模式有效）----
        {"key": "atr_period", "label": "ATR周期", "type": "int", "default": 14, "min": 5, "max": 30,
         "group": "做T·网格", "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "网格阈值 = ATR%/close × 网格ATR倍数"},
        {"key": "grid_atr_mult", "label": "网格ATR倍数", "type": "float", "default": 1.0,
         "min": 0.1, "max": 2, "step": 0.1, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "倍数越大网格越宽，触发越少、单笔幅度越大"},
        {"key": "grid_floor_pct", "label": "网格阈值下限", "type": "float", "default": 0.4,
         "min": 0.2, "max": 1.0, "step": 0.1, "unit": "%", "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "往返成本约0.07%+滑点，阈值低于此必亏，故设下限兜底"},
        {"key": "asym_bias", "label": "趋势非对称系数", "type": "float", "default": 0.3,
         "min": 0, "max": 0.6, "step": 0.1, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "上行时放宽卖出阈值、收窄买回（防卖飞），走弱时反之"},
        {"key": "t_ratio_base", "label": "T单比例基准", "type": "float", "default": 25,
         "min": 10, "max": 50, "step": 1, "unit": "%", "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "单次做T动用底仓的比例，再乘波动乘数与日内衰减"},
        {"key": "t_decay", "label": "T比例日内衰减", "type": "float", "default": 0.7,
         "min": 0.5, "max": 0.95, "step": 0.05, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "当日第 n 次 T 单比例 × t_decay^n（越晚越轻）"},
        {"key": "asym_sell_cap", "label": "卖飞保护乖离", "type": "float", "default": 2.0,
         "min": 0, "max": 6, "step": 0.1, "unit": "×ATR", "group": "做T·网格",
         "advanced": True, "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "乖离超过该值禁止网格卖出（仅买回），治强趋势卖飞"},
        # ---- G7 做T·波动定档：二次微调（高波放宽/低波收窄）----
        {"key": "vol_window", "label": "波动中位数窗口", "type": "int", "default": 120, "min": 30, "max": 250,
         "unit": "日", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 滚动分位的回看窗口，用于判断当前处于高波还是低波"},
        {"key": "vol_q_hi", "label": "高波分位数", "type": "float", "default": 0.7,
         "min": 0.5, "max": 0.95, "step": 0.05, "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 高于该分位视为高波"},
        {"key": "vol_q_lo", "label": "低波分位数", "type": "float", "default": 0.3,
         "min": 0.05, "max": 0.5, "step": 0.05, "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 低于该分位视为低波"},
        {"key": "vol_grid_hi", "label": "高波网格放宽", "type": "float", "default": 1.3,
         "min": 1.0, "max": 2.5, "step": 0.1, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "高波时网格阈值放宽倍数（防噪声打穿）"},
        {"key": "vol_grid_lo", "label": "低波网格收窄", "type": "float", "default": 0.8,
         "min": 0.5, "max": 1.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "低波时网格阈值收窄倍数（保证触发）"},
        {"key": "t_vol_hi", "label": "高波T比例乘数", "type": "float", "default": 1.3333,
         "min": 1.0, "max": 2.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "高波时 T 单比例乘数上限"},
        {"key": "t_vol_lo", "label": "低波T比例乘数", "type": "float", "default": 0.6667,
         "min": 0.3, "max": 1.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "低波时 T 单比例乘数下限"},
        # ---- G8 做T·机制专属：随 t_mode 切换显示 ----
        {"key": "t_debt_max_days", "label": "债务时限", "type": "int", "default": 2, "min": 1, "max": 10,
         "unit": "交易日", "group": "做T·机制专属",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "做T债务超过N交易日未回补 -> 作废转正式减仓"},
        {"key": "t_max_chase_pct", "label": "追回价格上限", "type": "float", "default": 3.0,
         "min": 0, "max": 20, "step": 0.5, "unit": "%", "group": "做T·机制专属",
         "show_if": {"t_mode": ["grid"]},
         "description": "grid模式：买回价高于卖出均价N%即放弃追回（封右尾）"},
        {"key": "reentry_discount", "label": "回补限价折让", "type": "float", "default": 1.0,
         "min": 0, "max": 10, "step": 0.1, "unit": "%", "group": "做T·机制专属",
         "show_if": {"t_mode": ["discipline"]},
         "description": "discipline模式：仅当价格回到卖出价下方N%才回补"},
        # ---- G9 风控·崩溃保护：防追高连板（σ自适应 + 绝对上限双保险）----
        {"key": "crash_sigma", "label": "动量崩溃阈值(σ)", "type": "float", "default": 2.0,
         "min": 1, "max": 4, "step": 0.5, "group": "崩溃保护", "advanced": True,
         "description": "近5日涨幅 > σ×自身波动√5 -> 动量分作废不入榜"},
        {"key": "crash_vol_n", "label": "崩溃波动窗口", "type": "int", "default": 60,
         "min": 20, "max": 120, "unit": "日", "group": "崩溃保护", "advanced": True},
        {"key": "crash_abs_cap", "label": "崩溃绝对涨幅上限", "type": "float", "default": 30,
         "min": 10, "max": 60, "step": 1, "unit": "%", "group": "崩溃保护", "advanced": True,
         "description": "近5日涨幅超此值硬性禁入（σ自适应阈值作第二道），"
                        "防高波股连板后σ阈值自动放宽而仍被满配"},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None) -> dict[str, pl.DataFrame]:
        p = {k["key"]: k["default"] for k in self.param_schema}
        p.update({k: v for k, v in (params or {}).items() if v is not None})

        # E 格：日线数据（date 无时间戳）无做T，硬关 max_t_times
        if data:
            first = next(iter(data.values()))
            if not bool(first["date"].str.contains(":").any()):
                p = dict(p)
                p["max_t_times"] = 0

        # 1. 每股日线特征
        feats = {code: self._daily_features(df, p) for code, df in data.items()}
        # 2. 横截面动量排名：day -> top_n 的 code 集合（T-1 语义，见 _rank_days）
        top_days = self._rank_days(feats, int(p["top_n"]))
        # 3. 每股状态机
        out: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
            # A1 点时可得性对齐（防未来函数）：
            # feats[code] 第 i 行的特征（day_i 收盘后才可知）整体后移一个交易日，
            # 当日 bar 只能"看见"上一完整交易日的特征，避免 09:35 就知道当日收盘。
            feats_t1 = feats[code].with_columns(
                pl.col("day").shift(-1).alias("day")).drop_nulls("day")
            df = df.join(feats_t1, on="day", how="left")
            cols = self._walk(df, p, top_days.get(code, set()), start_date)
            df = df.with_columns(cols)
            out[code] = df.drop("day")
        return out

    # ---------------- 日线特征（公式收敛于 momentum_core，与选股器同口径） ----------------

    @staticmethod
    def _daily_features(df: pl.DataFrame, p: dict) -> pl.DataFrame:
        """聚合日线并计算趋势/波动/动量特征，返回按 day 的特征表"""
        return mc.daily_feature_core(mc.aggregate_daily(df), p,
                                     anchor_key="trend_ma", anchor_name="ma_slow",
                                     with_accel=False)

    @staticmethod
    def _rank_days(feats: dict[str, pl.DataFrame], top_n: int) -> dict[str, set]:
        """每日按动量分排名，返回 code -> 可建仓日集合（T-1 语义，见 momentum_core.rank_days）"""
        return mc.rank_days(feats, top_n)

    # ---------------- 逐bar状态机 ----------------

    @staticmethod
    def _walk(df: pl.DataFrame, p: dict, top_days: set,
              start_date: str | None) -> list[pl.Series]:
        """生成 signal/tag/reason/budget_pct/t_ratio/reduce_pct 列"""
        n = df.height
        signals = [0] * n
        tags = [""] * n
        reasons = [""] * n
        budgets: list[float | None] = [None] * n
        t_ratios: list[float | None] = [None] * n
        reduces: list[float | None] = [None] * n

        base_min = float(p["base_pct_min"])
        base_max = float(p["base_pct_max"])
        mult = float(p["grid_atr_mult"])
        floor_g = float(p["grid_floor_pct"]) / 100.0
        asym = float(p["asym_bias"])
        t_base = float(p["t_ratio_base"]) / 100.0
        max_t = int(p["max_t_times"])
        t_mode = str(p.get("t_mode") or "grid")
        asym_sell_cap = float(p.get("asym_sell_cap") or 2.0)
        if t_mode == "off":
            max_t = 0  # 关闭做T层（C 基线）
        vol_grid_hi = float(p["vol_grid_hi"])
        vol_grid_lo = float(p["vol_grid_lo"])
        t_vol_hi = float(p["t_vol_hi"])
        t_vol_lo = float(p["t_vol_lo"])
        t_decay = float(p["t_decay"])
        max_adds = int(p["max_adds"])
        add_scale = float(p["add_scale"])
        add_cd = int(p["add_cooldown"])
        overheat_k = float(p["overheat_k"])
        reduce_pct = float(p["reduce_pct"])
        reduce_cd = int(p["reduce_cooldown"])

        cols = ["date", "close", "atr_pct", "bias", "vol_pos", "breakout",
                "dif", "dea", "ma_slow", "slope", "day_idx"]
        # trend_clock=daily：趋势信号只在当日最后一根bar评估（is_eod），次日开盘成交；
        # 做T网格不受门控，仍盘中逐bar运行（阈值用T-1 ATR/vol_pos，无泄漏）。
        trend_clock = str(p.get("trend_clock") or "intraday")
        dts = df["date"].to_list()
        is_eod = [i == n - 1 or dts[i][:10] != dts[i + 1][:10] for i in range(n)]
        opened = False
        full = False           # True=满配确认，False=试仓
        adds_done = 0
        last_add_idx = -10**9
        last_reduce_idx = -10**9
        cur_day = None
        ref = None
        t_count = 0

        for i, row in enumerate(df.select(cols).iter_rows()):
            (date, close, atr_pct, bias, vol_pos, breakout,
             dif, dea, ma_slow, slope, day_idx) = row
            day = date[:10]
            if start_date and day < start_date:
                continue  # 预热期：不推进状态机
            if day != cur_day:
                cur_day = day
                ref = None
                t_count = 0
            trend_ok = (trend_clock != "daily") or is_eod[i]

            macd_ok = dif is not None and dea is not None and dif > dea
            above = ma_slow is not None and close > ma_slow
            slope_up = slope is not None and slope > 0
            bear = (dif is not None and dea is not None and dif < dea) and not above
            confirmed = macd_ok and above and slope_up

            # ---- 1) 双确认翻空：清仓（最高优先级） ----
            if opened and bear and trend_ok:
                signals[i] = -1
                tags[i] = ""
                reasons[i] = "MACD死叉+跌破慢线，趋势翻空清仓"
                opened, full, adds_done = False, False, 0
                continue

            if not opened:
                # ---- 2) 建仓：初步确认试仓 / 三重确认满配 ----
                if macd_ok and above and day in top_days and trend_ok:
                    if confirmed:
                        budgets[i] = base_max
                        reasons[i] = "三重确认（金叉+站上慢线+斜率向上），满配建仓"
                    else:
                        budgets[i] = base_min
                        reasons[i] = "初步确认（金叉+站上慢线），试仓建仓"
                    signals[i] = 1
                    tags[i] = "开仓"
                    opened, full = True, confirmed
                continue

            # ---- 3) 试仓升级：确认升级后补到满配 ----
            if not full and confirmed and trend_ok:
                signals[i] = 1
                tags[i] = "加仓"
                budgets[i] = max(0.0, base_max - base_min)
                reasons[i] = "斜率确认，试仓升级满配"
                full = True
                continue

            # ---- 4) 金字塔加仓：突破新高 + 冷却期 + 次数递减 ----
            if (full and breakout and adds_done < max_adds
                    and (day_idx - last_add_idx) >= add_cd and trend_ok):
                budget = base_max * (add_scale ** (adds_done + 1))
                if budget >= 1.0:
                    signals[i] = 1
                    tags[i] = "加仓"
                    budgets[i] = budget
                    reasons[i] = f"突破{int(p['add_breakout_n'])}日新高，第{adds_done + 1}次金字塔加仓"
                    adds_done += 1
                    last_add_idx = day_idx
                    ref = close
                    continue

            # ---- 5) 过热减仓：乖离超阈值 + 冷却期 ----
            if (bias is not None and bias > overheat_k
                    and (day_idx - last_reduce_idx) >= reduce_cd and trend_ok):
                signals[i] = -1
                tags[i] = "减仓"
                reduces[i] = reduce_pct
                reasons[i] = f"乖离{bias:.1f}×ATR过热，减仓{reduce_pct:g}%锁盈"
                last_reduce_idx = day_idx
                ref = close
                continue

            # ---- 6) 做T：时点规律T(D) 或 ATR 自适应非对称网格 ----
            if t_mode == "time":
                # D：每日 09:35 高抛 1/4 底仓 / 14:50 尾盘买回（吃 A 股开盘冲高+尾盘低位规律）
                if " " not in date or t_count >= max_t:
                    continue
                hhmm = date[11:16]
                if hhmm == "09:35":
                    signals[i] = -1
                    tags[i] = "做T"
                    t_ratios[i] = 25.0
                    reasons[i] = "时点T：09:35高抛1/4底仓"
                    t_count += 1
                elif hhmm == "14:50":
                    signals[i] = 1
                    tags[i] = "做T"
                    budgets[i] = base_min
                    reasons[i] = "时点T：14:50尾盘买回"
                    t_count += 1
                continue
            if atr_pct is None or t_count >= max_t:
                continue
            g = float(atr_pct) * mult
            if g <= 0:
                continue
            # 波动状态调整：vol_pos(0~1) 线性插值，高波放宽(防噪声打穿)、低波收窄(保证触发)。
            # vol_pos 无分位数(样本不足)时按中性 0.5 处理，保持原比例。
            vp = vol_pos if vol_pos is not None else 0.5
            g *= vol_grid_lo + (vol_grid_hi - vol_grid_lo) * vp
            # 费用下限保护（往返成本约0.07%+滑点，阈值低于此必亏）
            g = max(g, floor_g)
            # 趋势非对称：强上行放宽卖出阈值（防卖飞）、收窄买回；走弱反之
            b = bias if bias is not None else 0.0
            g_sell = g * (1 + asym) if b > 0 else g * (1 - asym)
            g_buy = g * (1 - asym) if b > 0 else g * (1 + asym)
            # 动态T比例：波动线性插值 × 日内衰减（t_decay^n，随当日T次数递减）
            vol_mult = t_vol_lo + (t_vol_hi - t_vol_lo) * vp
            ratio = min(1.0, t_base * vol_mult * (t_decay ** t_count))

            if ref is None:
                ref = close
                continue
            if close <= ref * (1 - g_buy):
                signals[i] = 1
                tags[i] = "做T"
                t_ratios[i] = ratio * 100
                # 网格买点携带试仓档预算：引擎遇到空仓重建底仓时按 base_pct_min 封顶，
                # 避免走完整风控预算满仓重建（正常做T债务买回路径不读 budget_pct，不受影响）
                budgets[i] = base_min
                reasons[i] = f"跌破下网格线(阈值{g_buy * 100:.2f}%)买回"
                ref = close
                t_count += 1
            elif b <= asym_sell_cap and close >= ref * (1 + g_sell):
                signals[i] = -1
                tags[i] = "做T"
                t_ratios[i] = ratio * 100
                reasons[i] = f"升破上网格线(阈值{g_sell * 100:.2f}%)高抛"
                ref = close
                t_count += 1

        return [
            pl.Series("signal", signals, dtype=pl.Int32),
            pl.Series("tag", tags, dtype=pl.Utf8),
            pl.Series("reason", reasons, dtype=pl.Utf8),
            pl.Series("budget_pct", budgets, dtype=pl.Float64),
            pl.Series("t_ratio", t_ratios, dtype=pl.Float64),
            pl.Series("reduce_pct", reduces, dtype=pl.Float64),
        ]
