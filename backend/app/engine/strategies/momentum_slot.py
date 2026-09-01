# -*- coding: utf-8 -*-
"""动量槽位轮换策略（momentum_slot）：加速启动选股 + 个股衰退退出 + 网格做T降成本 + 槽位轮换。

架构（对照产品需求）：
- 选股层（横截面）：多周期风险调整动量 + 加速度项（短周期跑赢中周期=加速）打分，
  每日取前 pool_n 为候选池；引擎按风控 max_holdings 做槽位管理，持仓不足时自动补位。
- 个股状态机（逐股独立，不混组合）：
  ① 加速启动建仓：金叉 + 站上快均线(ma_fast) + 当日进入候选池 → 分档建仓
     （斜率向上满配 base_pct_max，否则试仓 base_pct_min）；
  ② 持仓做T：ATR 自适应非对称网格（完整复用 momentum_t 做T层），双向高抛低吸降成本；
     正向T(fwd_t=on)：逢低买入更多筹码→等反弹后高抛底仓；
  ③ 衰退初期退出（个股级）：MACD死叉/跌破MA20/动量衰减/转负/跌出榜单，
     三项中满足 ≥ exit_need 项即退出；渐进式：首次减partial_exit_pct→二次清仓；
     ATR硬止损(bias < atr_stop_k) 优先级最高，盘中实时触发；
  ④ 退出冷却：该股退出后 exit_cooldown 个交易日内不重建，杜绝
     "止损→立即买回→再止损"的放血循环；
- 止损兜底：由引擎风控承担（建议 stop_loss_mode=atr_trailing，ATR 移动止损锁盈）。

信号列协议：signal / tag / reason / budget_pct（开仓·加仓预算%）/
t_ratio（做T比例%）/ reduce_pct（减仓比例%）。
"""
import polars as pl

from .. import momentum_core as mc
from .ma_cross import Strategy


class MomentumSlotStrategy(Strategy):
    id = "momentum_slot"
    name = "动量槽位轮换"
    description = ("加速动量候选池选股 + 个股独立生命周期：加速启动建仓→网格做T降成本→"
                   "衰退初期(3取2)退出→冷却后从候选池补位。止损建议配合 ATR 移动止损。"
                   "适合5分钟周期，建议开启引擎预热。")
    periods = ["minute5", "daily"]  # daily 为 E 格（纯日线趋势层稳健性参考）

    @property
    def warmup_days(self) -> int:
        """指标预热建议值（交易日数）= 最长回看参数默认值 + 缓冲。"""
        lookback = {"ma_fast", "vol_window", "crash_vol_n", "mom_long",
                    "add_breakout_n", "slope_n"}
        longest = max((int(p["default"]) for p in self.param_schema
                       if p["key"] in lookback and p.get("default")), default=0)
        return longest + 180

    param_schema = [
        # ---- G1 核心开关 ----
        {"key": "t_mode", "label": "做T机制", "type": "categorical", "group": "核心开关",
         "choices": ["grid", "discipline", "time", "off"], "default": "grid",
         "description": "grid=网格+双止损(L1)；discipline=回补纪律(L2)；time=时点规律T(D)；"
                        "off=关闭做T(C)。选 off/time 时下方网格类参数自动隐藏"},
        {"key": "max_t_times", "label": "日内T次数上限", "type": "int", "default": 4, "min": 0, "max": 10,
         "group": "核心开关", "show_if": {"t_mode": ["grid", "discipline", "time"]},
         "description": "每日最多做T几次；0=关闭做T层。受风控 max_intraday_trades 二次约束"},
        {"key": "trend_clock", "label": "趋势时钟", "type": "categorical", "group": "核心开关",
         "choices": ["intraday", "daily"], "default": "intraday",
         "description": "intraday=盘中触发；daily=建仓/退出信号仅在当日末bar评估、次日开盘成交（做T不受限）"},
        {"key": "pool_n", "label": "候选池大小", "type": "int", "default": 6, "min": 1, "max": 20,
         "group": "核心开关",
         "description": "横截面加速动量排名前 pool_n 为候选池；建议 ≥ 风控 max_holdings（持仓不足自动补位）"},
        {"key": "max_holdings", "label": "最大持仓只数", "type": "int", "default": 3, "min": 1, "max": 10,
         "group": "核心开关",
         "description": "同时最多持仓N只，满仓不新增，退出后从候选池自动补位；0=不限（兼容旧版）"},
        # ---- G0 止损（最高优先级，盘中实时触发）----
        {"key": "atr_stop_k", "label": "ATR硬止损倍数", "type": "float", "default": -3.0,
         "min": -6, "max": -1, "step": 0.5, "unit": "×ATR", "group": "止损",
         "description": "bias < 此值触发ATR硬止损（价格低于快均线N倍ATR），盘中实时执行，优先级最高"},
        # ---- G2 选股排序（加速动量）----
        {"key": "mom_short", "label": "短周期动量", "type": "int", "default": 10, "min": 5, "max": 40,
         "unit": "日", "group": "选股排序"},
        {"key": "mom_mid", "label": "中周期动量", "type": "int", "default": 60, "min": 30, "max": 90,
         "unit": "日", "group": "选股排序"},
        {"key": "mom_long", "label": "长周期动量", "type": "int", "default": 120, "min": 90, "max": 200,
         "unit": "日", "group": "选股排序"},
        {"key": "w_short", "label": "短周期权重", "type": "float", "default": 0.5, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序"},
        {"key": "w_mid", "label": "中周期权重", "type": "float", "default": 0.3, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序", "description": "长周期权重 = 1 - 短 - 中"},
        {"key": "w_accel", "label": "加速项权重", "type": "float", "default": 0.3, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序",
         "description": "短周期动量 − 中周期动量（短期跑赢中期=加速段），刻画“加速启动期”"},
        {"key": "crash_sigma", "label": "动量崩溃阈值(σ)", "type": "float", "default": 2.0,
         "min": 1, "max": 4, "step": 0.5, "group": "选股排序", "advanced": True,
         "description": "近5日涨幅 > σ×自身波动√5 -> 动量分作废不入榜"},
        {"key": "crash_vol_n", "label": "崩溃波动窗口", "type": "int", "default": 60,
         "min": 20, "max": 120, "unit": "日", "group": "选股排序", "advanced": True},
        {"key": "crash_abs_cap", "label": "崩溃绝对涨幅上限", "type": "float", "default": 30,
         "min": 10, "max": 60, "step": 1, "unit": "%", "group": "选股排序", "advanced": True,
         "description": "近5日涨幅超此值硬性禁入（σ自适应阈值作第二道），防连板追高"},
        # ---- G3 趋势判据（建仓确认 / 退出信号）----
        {"key": "macd_fast", "label": "MACD快线", "type": "int", "default": 12, "min": 5, "max": 30,
         "group": "趋势判据"},
        {"key": "macd_slow", "label": "MACD慢线", "type": "int", "default": 26, "min": 10, "max": 60,
         "group": "趋势判据"},
        {"key": "macd_signal", "label": "MACD信号线", "type": "int", "default": 9, "min": 3, "max": 20,
         "group": "趋势判据"},
        {"key": "ma_fast", "label": "快均线周期", "type": "int", "default": 20, "min": 5, "max": 60,
         "group": "趋势判据",
         "description": "站上/跌破该均线为建仓确认与衰退退出信号（比慢线灵敏）"},
        {"key": "slope_n", "label": "斜率确认窗口", "type": "int", "default": 5, "min": 2, "max": 10,
         "group": "趋势判据", "description": "均线斜率向上才算满配确认（试仓升级的触发条件）"},
        # ---- G4 建仓与加仓 ----
        {"key": "base_pct_min", "label": "试仓资金占比", "type": "float", "default": 10,
         "min": 5, "max": 40, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "金叉+站上快均线+入榜但斜率未确认时的首仓比例"},
        {"key": "base_pct_max", "label": "满配资金占比", "type": "float", "default": 50,
         "min": 30, "max": 90, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "加速确认（斜率向上）后的目标仓位；实际仍受风控个股上限约束"},
        {"key": "max_adds", "label": "最大加仓次数", "type": "int", "default": 2, "min": 0, "max": 4,
         "group": "建仓与加仓"},
        {"key": "add_scale", "label": "加仓规模递减系数", "type": "float", "default": 0.5,
         "min": 0.2, "max": 0.8, "step": 0.1, "group": "建仓与加仓",
         "description": "第 n 次加仓预算 = 满配 × 系数^n（金字塔越加越小）"},
        {"key": "add_cooldown", "label": "加仓冷却期", "type": "int", "default": 5, "min": 1, "max": 20,
         "unit": "交易日", "group": "建仓与加仓"},
        {"key": "add_breakout_n", "label": "新高突破窗口", "type": "int", "default": 20, "min": 5, "max": 60,
         "unit": "日", "group": "建仓与加仓", "description": "创 N 日新高才允许金字塔加仓"},
        # ---- G5 退出（衰退初期，个股级）----
        {"key": "exit_need", "label": "衰退信号满足数", "type": "int", "default": 2, "min": 1, "max": 3,
         "group": "衰退退出",
         "description": "MACD死叉/跌破MA20/动量转负或跌出榜单，满足 ≥N 项即退出（2=更保险）"},
        {"key": "exit_cooldown", "label": "退出冷却期", "type": "int", "default": 5, "min": 0, "max": 20,
         "unit": "交易日", "group": "衰退退出",
         "description": "该股退出后 N 个交易日内不重建，防\"止损->立即买回->再止损\"放血"},
        {"key": "decay_window", "label": "动量衰减窗口", "type": "int", "default": 5,
         "min": 2, "max": 20, "unit": "日", "group": "衰退退出",
         "description": "滚动窗口内score峰值，score从峰值回落超decay_pct即判定动量衰减"},
        {"key": "decay_pct", "label": "动量衰减阈值", "type": "float", "default": 0.15,
         "min": 0.05, "max": 0.5, "step": 0.05, "group": "衰退退出",
         "description": "score从峰值回落超过此比例即判定动量衰减（早于score<0触发）"},
        {"key": "partial_exit_pct", "label": "首次减仓比例", "type": "float", "default": 50,
         "min": 10, "max": 80, "step": 5, "unit": "%", "group": "衰退退出",
         "description": "衰退初期首次减仓比例，剩余仓位待二次信号清仓（渐进式退出）"},
        # ---- G7 做T·正向T ----
        {"key": "fwd_t", "label": "正向T开关", "type": "categorical", "group": "做T·正向T",
         "choices": ["off", "on"], "default": "off",
         "description": "on=允许正向T：逢低买入更多筹码→等反弹后高抛底仓降成本（需底仓存在）"},
        {"key": "fwd_t_budget_pct", "label": "正向T买入占比", "type": "float", "default": 25,
         "min": 5, "max": 50, "step": 5, "unit": "%", "group": "做T·正向T",
         "show_if": {"fwd_t": ["on"]},
         "description": "正向T逢低买入时动用的资金占比"},
        # ---- G6 做T·网格（完整复用 momentum_t 做T层）----
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
        # ---- G7 做T·波动定档（二次微调）----
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
        # ---- G8 做T·机制专属（随 t_mode 切换显示）----
        {"key": "t_debt_max_days", "label": "债务时限", "type": "int", "default": 3, "min": 1, "max": 10,
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
        # 2. 横截面加速动量排名：day -> 候选池 code 集合（T-1 语义，见 _rank_days）
        top_days = self._rank_days(feats, int(p["pool_n"]))
        # 3. 每股状态机
        out: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
            # A1 点时可得性对齐（防未来函数）：特征整体后移一个交易日，
            # 当日 bar 只能"看见"上一完整交易日的特征，避免 09:35 就知道当日收盘。
            feats_t1 = feats[code].with_columns(
                pl.col("day").shift(-1).alias("day")).drop_nulls("day")
            df = df.join(feats_t1, on="day", how="left")
            cols = self._walk(df, p, top_days.get(code, set()), start_date)
            df = df.with_columns(cols)
            out[code] = df.drop("day")

        # 4. 槽位管理后处理：max_holdings 强制约束
        max_holdings = int(p.get("max_holdings", 0))
        if max_holdings > 0:
            out = self._enforce_slots(out, max_holdings)

        return out

    # ---------------- 日线特征（公式收敛于 momentum_core，与选股器同口径） ----------------

    @staticmethod
    def _daily_features(df: pl.DataFrame, p: dict) -> pl.DataFrame:
        """聚合日线并计算趋势/波动/加速动量特征，返回按 day 的特征表"""
        return mc.daily_feature_core(mc.aggregate_daily(df), p,
                                     anchor_key="ma_fast", anchor_name="ma_fast",
                                     with_accel=True)

    @staticmethod
    def _rank_days(feats: dict[str, pl.DataFrame], pool_n: int) -> dict[str, set]:
        """每日按加速动量分排名，返回 code -> 可建仓日集合（T-1 语义，见 momentum_core.rank_days）"""
        return mc.rank_days(feats, pool_n)

    # ---------------- 逐bar状态机 ----------------

    @staticmethod
    def _walk(df: pl.DataFrame, p: dict, top_days: set,
              start_date: str | None) -> list[pl.Series]:
        """生成 signal/tag/reason/budget_pct/t_ratio/reduce_pct 列

        状态机优先级（从高到低）：
          0) ATR硬止损（盘中实时，最高优先级）
          1) 衰退初期退出（渐进式：首次减partial_exit_pct->二次清仓）
          2) 加速启动建仓（金叉+站上快均线+候选池+冷却期）
          3) 试仓升级（斜率确认->满配）
          4) 金字塔加仓（突破新高+冷却+递减）
          5) 做T（正向T逢低买入 / 反向T高抛低吸）
        """
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
            max_t = 0
        vol_grid_hi = float(p["vol_grid_hi"])
        vol_grid_lo = float(p["vol_grid_lo"])
        t_vol_hi = float(p["t_vol_hi"])
        t_vol_lo = float(p["t_vol_lo"])
        t_decay = float(p["t_decay"])
        max_adds = int(p["max_adds"])
        add_scale = float(p["add_scale"])
        add_cd = int(p["add_cooldown"])
        exit_need = int(p["exit_need"])
        exit_cd = int(p["exit_cooldown"])
        # ---- 新增参数 ----
        atr_stop_k = float(p.get("atr_stop_k") or -3.0)
        decay_window = int(p.get("decay_window") or 5)
        decay_pct = float(p.get("decay_pct") or 0.15)
        partial_exit_pct = float(p.get("partial_exit_pct") or 50)
        fwd_t = str(p.get("fwd_t") or "off")
        fwd_t_budget = float(p.get("fwd_t_budget_pct") or 25) / 100.0

        cols = ["date", "close", "atr_pct", "bias", "vol_pos", "breakout",
                "dif", "dea", "ma_fast", "slope", "score", "day_idx"]
        trend_clock = str(p.get("trend_clock") or "intraday")
        dts = df["date"].to_list()
        is_eod = [i == n - 1 or dts[i][:10] != dts[i + 1][:10] for i in range(n)]
        opened = False
        full = False
        adds_done = 0
        last_add_idx = -10**9
        last_exit_idx = -10**9
        cur_day = None
        ref = None
        t_count = 0
        # 动量衰减跟踪：持仓期间score峰值
        score_peak: float | None = None
        # 渐进式退出阶段: 0=无退出, 1=已部分减仓待清仓, 2=已清仓
        exit_stage = 0

        for i, row in enumerate(df.select(cols).iter_rows()):
            (date, close, atr_pct, bias, vol_pos, breakout,
             dif, dea, ma_fast, slope, score, day_idx) = row
            day = date[:10]
            if start_date and day < start_date:
                continue
            if day != cur_day:
                cur_day = day
                ref = None
                t_count = 0
                score_peak = None
            trend_ok = (trend_clock != "daily") or is_eod[i]

            macd_ok = dif is not None and dea is not None and dif > dea
            above_fast = ma_fast is not None and close > ma_fast
            slope_up = slope is not None and slope > 0
            # 衰退初期三信号（个股级，T-1 特征，次日成交）
            s1 = dif is not None and dea is not None and dif < dea
            s2 = ma_fast is not None and close < ma_fast
            # 动量衰减：持仓期间score从峰值回落超过decay_pct
            if opened and score is not None:
                if score_peak is None or score > score_peak:
                    score_peak = score
                score_decay = (score_peak is not None
                               and score < score_peak * (1 - decay_pct))
            else:
                score_decay = False
            s3 = ((score is not None and score < 0)
                  or (day not in top_days)
                  or score_decay)

            # ---- 0) ATR硬止损（最高优先级，盘中实时触发） ----
            if opened and bias is not None and bias < atr_stop_k:
                signals[i] = -1
                tags[i] = "止损"
                reasons[i] = (f"ATR硬止损(bias={bias:.1f}<-{atr_stop_k})"
                              f" 价格跌破{atr_stop_k}倍ATR")
                opened, full, adds_done = False, False, 0
                exit_stage = 0
                continue

            # ---- 1) 衰退初期退出：渐进式（首次减仓->二次清仓） ----
            if opened and trend_ok and (int(s1) + int(s2) + int(s3)) >= exit_need:
                hits = []
                if s1: hits.append("MACD死叉")
                if s2: hits.append(f"跌破MA{int(p['ma_fast'])}")
                if score_decay: hits.append("动量衰减")
                if score is not None and score < 0: hits.append("动量转负")
                if day not in top_days: hits.append("跌出榜单")
                if exit_stage == 0:
                    signals[i] = -1
                    tags[i] = "减仓"
                    reduces[i] = partial_exit_pct
                    reasons[i] = (f"衰退初期(首次减{partial_exit_pct:.0f}%): "
                                  f"{'+'.join(hits)}")
                    exit_stage = 1
                    continue
                elif exit_stage == 1:
                    signals[i] = -1
                    tags[i] = ""
                    reasons[i] = f"衰退清仓(二次): {'+'.join(hits)}"
                    opened, full, adds_done = False, False, 0
                    exit_stage = 2
                    last_exit_idx = day_idx
                    continue

            if not opened:
                # ---- 2) 加速启动建仓 ----
                if (macd_ok and above_fast and day in top_days
                        and (day_idx - last_exit_idx) >= exit_cd and trend_ok):
                    if slope_up:
                        budgets[i] = base_max
                        reasons[i] = ("加速启动(金叉+站上快均线+入榜+斜率向上)，满配建仓")
                    else:
                        budgets[i] = base_min
                        reasons[i] = ("加速启动(金叉+站上快均线+入榜)，试仓建仓")
                    signals[i] = 1
                    tags[i] = "开仓"
                    opened, full = True, slope_up
                continue

            # ---- 3) 试仓升级 ----
            if not full and slope_up and trend_ok:
                signals[i] = 1
                tags[i] = "加仓"
                budgets[i] = max(0.0, base_max - base_min)
                reasons[i] = "斜率确认，试仓升级满配"
                full = True
                continue

            # ---- 4) 金字塔加仓 ----
            if (full and breakout and adds_done < max_adds
                    and (day_idx - last_add_idx) >= add_cd and trend_ok):
                budget = base_max * (add_scale ** (adds_done + 1))
                if budget >= 1.0:
                    signals[i] = 1
                    tags[i] = "加仓"
                    budgets[i] = budget
                    reasons[i] = (f"突破{int(p['add_breakout_n'])}日新高，"
                                  f"第{adds_done + 1}次金字塔加仓")
                    adds_done += 1
                    last_add_idx = day_idx
                    ref = close
                    continue

            # ---- 5) 做T ----
            if t_mode == "time":
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
            vp = vol_pos if vol_pos is not None else 0.5
            g *= vol_grid_lo + (vol_grid_hi - vol_grid_lo) * vp
            g = max(g, floor_g)
            b = bias if bias is not None else 0.0
            g_sell = g * (1 + asym) if b > 0 else g * (1 - asym)
            g_buy = g * (1 - asym) if b > 0 else g * (1 + asym)
            vol_mult = t_vol_lo + (t_vol_hi - t_vol_lo) * vp
            ratio = min(1.0, t_base * vol_mult * (t_decay ** t_count))

            if ref is None:
                ref = close
                continue

            # ---- 正向T：逢低买入 ----
            if fwd_t == "on" and close <= ref * (1 - g_buy) and opened:
                signals[i] = 1
                tags[i] = "做T"
                t_ratios[i] = ratio * 100
                budgets[i] = fwd_t_budget * 100
                reasons[i] = (f"正向T：跌破下网格线(阈值{g_buy * 100:.2f}%)"
                              f"逢低买入(预算{fwd_t_budget*100:.0f}%)")
                ref = close
                t_count += 1
                continue

            # ---- 反向T ----
            if close <= ref * (1 - g_buy):
                signals[i] = 1
                tags[i] = "做T"
                t_ratios[i] = ratio * 100
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

    @staticmethod
    def _enforce_slots(data: dict[str, pl.DataFrame],
                       max_holdings: int) -> dict[str, pl.DataFrame]:
        """槽位管理后处理：强制max_holdings约束。

        原理：
        - 遍历全局交易日历，按时间顺序推进
        - 维护当前持仓数(open_count)和每只股票的持仓状态
        - 当某股票产生建仓信号(open=1)且open_count >= max_holdings时，
          将该信号置0（释放槽位给更高分的候选股）
        - 当某股票产生退出信号(open=-1)时，open_count减1

        注意：此为策略层近似实现，引擎风控(max_holdings)仍为最终屏障。
        """
        if max_holdings <= 0 or not data:
            return data

        # 收集所有日期（按时间排序）
        all_dates: set[str] = set()
        for code, df in data.items():
            all_dates.update(
                df["date"].str.slice(0, 10).unique().to_list())
        sorted_dates = sorted(all_dates)

        # 预构建每只股票的 signal/tag/score 列表（按 row order）
        code_signals: dict[str, list[int]] = {}
        code_tags: dict[str, list[str]] = {}
        code_scores: dict[str, list[float]] = {}
        code_days: dict[str, list[str]] = {}
        for code, df in data.items():
            code_signals[code] = df["signal"].to_list()
            code_tags[code] = df["tag"].to_list()
            code_scores[code] = df["score"].to_list()
            code_days[code] = df["date"].str.slice(0, 10).to_list()

        held: dict[str, bool] = {}
        open_count = 0

        for day in sorted_dates:
            # 先处理退出信号（释放槽位）。
            # 做T高抛(sig=-1,tag=做T)只是同持仓内部减筹码，不释放槽位。
            for code in data:
                days = code_days[code]
                sigs = code_signals[code]
                tags = code_tags[code]
                for i, d in enumerate(days):
                    if d == day and sigs[i] == -1 and tags[i] != "做T" and held.get(code, False):
                        held[code] = False
                        open_count = max(0, open_count - 1)

            # 再处理建仓信号（检查槽位，按score降序）。
            # 做T买回(sig=1,tag=做T)是回补已有持仓的债务，不占新槽位、不参与竞争，
            # 绝不因槽位已满被置零（否则高抛后永远买不回来，债务拖到期末）。
            entries = []
            for code in data:
                days = code_days[code]
                sigs = code_signals[code]
                tags = code_tags[code]
                scores = code_scores[code]
                for i, d in enumerate(days):
                    if d == day and sigs[i] == 1 and tags[i] != "做T" and not held.get(code, False):
                        sc = scores[i]
                        if sc is None:
                            continue  # 跳过无score的条目（如崩溃保护期）
                        entries.append((-sc, code, i))  # 负分用于升序排序
            entries.sort()
            for neg_score, code, idx in entries:
                if open_count >= max_holdings:
                    # 槽位已满：将此bar的signal置0
                    data[code] = data[code].with_columns(
                        pl.when(pl.col("date").str.slice(0, 10) == day)
                          .then(0)
                          .otherwise(pl.col("signal"))
                          .alias("signal")
                    )
                    # 更新本地缓存
                    code_signals[code][idx] = 0
                else:
                    open_count += 1
                    held[code] = True

        return data

