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
         "choices": ["grid|网格（双止损）", "discipline|回补纪律", "time|时点规律T", "off|关闭做T"],
         "default": "grid",
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
         "unit": "日", "group": "选股排序",
         "description": "universe_auto 时选池预筛同步用此值（预筛与排名同尺）"},
        {"key": "mom_mid", "label": "中周期动量", "type": "int", "default": 60, "min": 30, "max": 90,
         "unit": "日", "group": "选股排序",
         "description": "universe_auto 时选池预筛同步用此值（预筛与排名同尺）"},
        {"key": "mom_long", "frozen": True, "label": "长周期动量", "type": "int", "default": 120, "min": 90, "max": 200,
         "unit": "日", "group": "选股排序"},
        {"key": "w_short", "label": "短周期权重", "type": "float", "default": 0.5, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序",
         "description": "universe_auto 时选池预筛同步用此值（预筛与排名同尺）"},
        {"key": "w_mid", "label": "中周期权重", "type": "float", "default": 0.3, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序", "description": "长周期权重 = 1 - 短 - 中；universe_auto 时预筛同步"},
        {"key": "w_accel", "label": "加速项权重", "type": "float", "default": 0.3, "min": 0, "max": 1,
         "step": 0.1, "group": "选股排序",
         "description": "短周期动量 − 中周期动量（短期跑赢中期=加速段），刻画“加速启动期”；universe_auto 时预筛同步（预筛是否启用加速度由 auto_with_accel 单独控制）"},
        {"key": "crash_sigma", "frozen": True, "label": "动量崩溃阈值(σ)", "type": "float", "default": 2.0,
         "min": 1, "max": 4, "step": 0.5, "group": "选股排序", "advanced": True,
         "description": "近5日涨幅 > σ×自身波动√5 -> 动量分作废不入榜"},
        {"key": "crash_vol_n", "frozen": True, "label": "崩溃波动窗口", "type": "int", "default": 60,
         "min": 20, "max": 120, "unit": "日", "group": "选股排序", "advanced": True},
        {"key": "crash_abs_cap", "frozen": True, "label": "崩溃绝对涨幅上限", "type": "float", "default": 30,
         "min": 10, "max": 60, "step": 1, "unit": "%", "group": "选股排序", "advanced": True,
         "description": "近5日涨幅超此值硬性禁入（σ自适应阈值作第二道），防连板追高"},
        # ---- G3 趋势判据（建仓确认 / 退出信号）----
        {"key": "macd_fast", "frozen": True, "label": "MACD快线", "type": "int", "default": 12, "min": 5, "max": 30,
         "group": "趋势判据"},
        {"key": "macd_slow", "frozen": True, "label": "MACD慢线", "type": "int", "default": 26, "min": 10, "max": 60,
         "group": "趋势判据"},
        {"key": "macd_signal", "frozen": True, "label": "MACD信号线", "type": "int", "default": 9, "min": 3, "max": 20,
         "group": "趋势判据"},
        {"key": "ma_fast", "frozen": True, "label": "快均线周期", "type": "int", "default": 20, "min": 5, "max": 60,
         "group": "趋势判据",
         "description": "站上/跌破该均线为建仓确认与衰退退出信号（比慢线灵敏）"},
        {"key": "slope_n", "frozen": True, "label": "斜率确认窗口", "type": "int", "default": 5, "min": 2, "max": 10,
         "group": "趋势判据", "description": "均线斜率向上才算满配确认（试仓升级的触发条件）"},
        # ---- G4 建仓与加仓 ----
        {"key": "base_pct_min", "frozen": True, "label": "试仓资金占比", "type": "float", "default": 10,
         "min": 5, "max": 40, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "金叉+站上快均线+入榜但斜率未确认时的首仓比例"},
        {"key": "base_pct_max", "label": "满配资金占比", "type": "float", "default": 50,
         "min": 5, "max": 90, "step": 1, "unit": "%", "group": "建仓与加仓",
         "description": "加速确认（斜率向上）后的目标仓位；实际仍受风控个股上限约束"},
        {"key": "max_adds", "frozen": True, "label": "最大加仓次数", "type": "int", "default": 2, "min": 0, "max": 4,
         "group": "建仓与加仓"},
        {"key": "add_scale", "frozen": True, "label": "加仓规模递减系数", "type": "float", "default": 0.5,
         "min": 0.2, "max": 0.8, "step": 0.1, "group": "建仓与加仓",
         "description": "第 n 次加仓预算 = 满配 × 系数^n（金字塔越加越小）"},
        {"key": "add_cooldown", "frozen": True, "label": "加仓冷却期", "type": "int", "default": 5, "min": 1, "max": 20,
         "unit": "交易日", "group": "建仓与加仓"},
        {"key": "add_breakout_n", "frozen": True, "label": "新高突破窗口", "type": "int", "default": 20, "min": 5, "max": 60,
         "unit": "日", "group": "建仓与加仓", "description": "创 N 日新高才允许金字塔加仓"},
        # ---- G5 退出（衰退初期，个股级）----
        {"key": "exit_need", "frozen": True, "label": "衰退信号满足数", "type": "int", "default": 2, "min": 1, "max": 3,
         "group": "衰退退出",
         "description": "MACD死叉/跌破MA20/动量转负或跌出榜单，满足 ≥N 项即退出（2=更保险）"},
        {"key": "exit_cooldown", "frozen": True, "label": "退出冷却期", "type": "int", "default": 5, "min": 0, "max": 20,
         "unit": "交易日", "group": "衰退退出",
         "description": "该股退出后 N 个交易日内不重建，防\"止损->立即买回->再止损\"放血"},
        {"key": "decay_window", "frozen": True, "label": "动量衰减窗口", "type": "int", "default": 5,
         "min": 2, "max": 20, "unit": "日", "group": "衰退退出",
         "description": "滚动窗口内score峰值，score从峰值回落超decay_pct即判定动量衰减"},
        {"key": "decay_pct", "frozen": True, "label": "动量衰减阈值", "type": "float", "default": 0.15,
         "min": 0.05, "max": 0.5, "step": 0.05, "group": "衰退退出",
         "description": "score从峰值回落超过此比例即判定动量衰减（早于score<0触发）"},
        {"key": "partial_exit_pct", "frozen": True, "label": "首次减仓比例", "type": "float", "default": 50,
         "min": 10, "max": 80, "step": 5, "unit": "%", "group": "衰退退出",
         "description": "衰退初期首次减仓比例，剩余仓位待二次信号清仓（渐进式退出）"},
        {"key": "exit_confirm_days", "frozen": True, "label": "二清确认期", "type": "int", "default": 0,
         "min": 0, "max": 10, "unit": "交易日", "group": "衰退退出",
         "description": "首减后进入确认期：期内收复快均线且重新入榜则取消二清；出现新死叉立即二清；期满仍弱才二清。0=关闭（保持原行为，首减后下一bar即清）"},
        {"key": "out_top_days", "frozen": True, "label": "跌出榜单确认日", "type": "int", "default": 0,
         "min": 0, "max": 5, "unit": "交易日", "group": "衰退退出",
         "description": "连续 N 日不在候选榜才计一次「跌出榜单」信号（事件化，不再每天重复触发）。0=原行为（每天不在榜即信号）"},
        {"key": "momentum_fsm_on", "frozen": True, "label": "动量状态机", "type": "categorical", "group": "衰退退出",
         "choices": ["off", "on"], "default": "off",
         "description": "on=用动量状态机（减速/衰竭）替代「3信号凑数」判定退出；off=沿用现有衰退信号"},
        {"key": "exit_fade_days", "frozen": True, "label": "衰竭确认日", "type": "int", "default": 2,
         "min": 0, "max": 10, "unit": "交易日", "group": "衰退退出",
         "description": "动量状态机模式下，连续衰竭 N 个交易日确认二清（默认2，给首减后1天缓冲）"},
        # ---- G7 做T·正向T ----
        {"key": "fwd_t", "label": "正向T开关", "type": "categorical", "group": "做T·正向T",
         "choices": ["off", "on"], "default": "off",
         "description": "on=允许正向T：逢低买入更多筹码→等反弹后高抛底仓降成本（需底仓存在）"},
        {"key": "fwd_t_budget_pct", "frozen": True, "label": "正向T买入占比", "type": "float", "default": 25,
         "min": 5, "max": 50, "step": 5, "unit": "%", "group": "做T·正向T",
         "show_if": {"fwd_t": ["on"]},
         "description": "正向T逢低买入时动用的资金占比"},
        # ---- G6 做T·网格（完整复用 momentum_t 做T层）----
        {"key": "atr_period", "frozen": True, "label": "ATR周期", "type": "int", "default": 14, "min": 5, "max": 30,
         "group": "做T·网格", "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "网格阈值 = ATR%/close × 网格ATR倍数"},
        {"key": "grid_atr_mult", "label": "网格ATR倍数", "type": "float", "default": 1.0,
         "min": 0.1, "max": 2, "step": 0.1, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "倍数越大网格越宽，触发越少、单笔幅度越大"},
        {"key": "grid_floor_pct", "frozen": True, "label": "网格阈值下限", "type": "float", "default": 0.4,
         "min": 0.2, "max": 1.0, "step": 0.1, "unit": "%", "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "往返成本约0.07%+滑点，阈值低于此必亏，故设下限兜底"},
        {"key": "asym_bias", "frozen": True, "label": "趋势非对称系数", "type": "float", "default": 0.3,
         "min": 0, "max": 0.6, "step": 0.1, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "上行时放宽卖出阈值、收窄买回（防卖飞），走弱时反之"},
        {"key": "t_ratio_base", "label": "T单比例基准", "type": "float", "default": 25,
         "min": 10, "max": 50, "step": 1, "unit": "%", "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "单次做T动用底仓的比例，再乘波动乘数与日内衰减"},
        {"key": "t_decay", "frozen": True, "label": "T比例日内衰减", "type": "float", "default": 0.7,
         "min": 0.5, "max": 0.95, "step": 0.05, "group": "做T·网格",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "当日第 n 次 T 单比例 × t_decay^n（越晚越轻）"},
        {"key": "asym_sell_cap", "frozen": True, "label": "卖飞保护乖离", "type": "float", "default": 2.0,
         "min": 0, "max": 6, "step": 0.1, "unit": "×ATR", "group": "做T·网格",
         "advanced": True, "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "乖离超过该值禁止网格卖出（仅买回），治强趋势卖飞"},
        # ---- G7 做T·波动定档（二次微调）----
        {"key": "vol_window", "frozen": True, "label": "波动中位数窗口", "type": "int", "default": 120, "min": 30, "max": 250,
         "unit": "日", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 滚动分位的回看窗口，用于判断当前处于高波还是低波"},
        {"key": "vol_q_hi", "frozen": True, "label": "高波分位数", "type": "float", "default": 0.7,
         "min": 0.5, "max": 0.95, "step": 0.05, "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 高于该分位视为高波"},
        {"key": "vol_q_lo", "frozen": True, "label": "低波分位数", "type": "float", "default": 0.3,
         "min": 0.05, "max": 0.5, "step": 0.05, "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "ATR% 低于该分位视为低波"},
        {"key": "vol_grid_hi", "frozen": True, "label": "高波网格放宽", "type": "float", "default": 1.3,
         "min": 1.0, "max": 2.5, "step": 0.1, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "高波时网格阈值放宽倍数（防噪声打穿）"},
        {"key": "vol_grid_lo", "frozen": True, "label": "低波网格收窄", "type": "float", "default": 0.8,
         "min": 0.5, "max": 1.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "低波时网格阈值收窄倍数（保证触发）"},
        {"key": "t_vol_hi", "frozen": True, "label": "高波T比例乘数", "type": "float", "default": 1.3333,
         "min": 1.0, "max": 2.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "高波时 T 单比例乘数上限"},
        {"key": "t_vol_lo", "frozen": True, "label": "低波T比例乘数", "type": "float", "default": 0.6667,
         "min": 0.3, "max": 1.0, "step": 0.05, "unit": "×", "group": "做T·波动定档", "advanced": True,
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "低波时 T 单比例乘数下限"},
        # ---- G8 做T·机制专属（随 t_mode 切换显示）----
        {"key": "t_debt_max_days", "frozen": True, "label": "债务时限", "type": "int", "default": 3, "min": 1, "max": 10,
         "unit": "交易日", "group": "做T·机制专属",
         "show_if": {"t_mode": ["grid", "discipline"]},
         "description": "做T债务超过N交易日未回补 -> 作废转正式减仓"},
        {"key": "t_max_chase_pct", "frozen": True, "label": "追回价格上限", "type": "float", "default": 3.0,
         "min": 0, "max": 20, "step": 0.5, "unit": "%", "group": "做T·机制专属",
         "show_if": {"t_mode": ["grid"]},
         "description": "grid模式：买回价高于卖出均价N%即放弃追回（封右尾）"},
        {"key": "reentry_discount", "frozen": True, "label": "回补限价折让", "type": "float", "default": 1.0,
         "min": 0, "max": 10, "step": 0.1, "unit": "%", "group": "做T·机制专属",
         "show_if": {"t_mode": ["discipline"]},
         "description": "discipline模式：仅当价格回到卖出价下方N%才回补"},
        # ---- 方案A：市场状态三态（trend/range/crash，指数日线判定，T-1 对齐） ----
        {"key": "market_regime_on", "frozen": True, "label": "市场状态开关", "type": "categorical", "group": "市场状态",
         "choices": ["off", "on"], "default": "off",
         "description": "on=按市场状态三态调整核心仓预算与做T频率（趋势/震荡/防守）；off=沿用现状"},
        {"key": "regime_index", "frozen": True, "label": "判定指数", "type": "categorical", "group": "市场状态",
         "choices": ["000905", "000300"], "default": "000905",
         "description": "中证500(000905) 贴近动量池中小盘；沪深300(000300) 偏大盘蓝筹"},
        {"key": "core_scale_range", "frozen": True, "label": "震荡核心仓系数", "type": "float", "default": 0.7,
         "min": 0.2, "max": 1.0, "step": 0.05, "unit": "×", "group": "市场状态",
         "description": "震荡市核心仓预算 × 系数（趋势弱，少配核心仓）"},
        {"key": "t_scale_range", "frozen": True, "label": "震荡做T频率系数", "type": "float", "default": 1.3,
         "min": 0.5, "max": 2.0, "step": 0.05, "unit": "×", "group": "市场状态",
         "description": "震荡市做T次数上限 × 系数（波动主场，提升做T）"},
        {"key": "core_scale_crash", "frozen": True, "label": "防守核心仓系数", "type": "float", "default": 0.4,
         "min": 0.1, "max": 1.0, "step": 0.05, "unit": "×", "group": "市场状态",
         "description": "防守市核心仓预算 × 系数（大幅收缩避险）"},
        {"key": "t_scale_crash", "frozen": True, "label": "防守做T频率系数", "type": "float", "default": 0.5,
         "min": 0.1, "max": 1.0, "step": 0.05, "unit": "×", "group": "市场状态",
         "description": "防守市做T次数上限 × 系数（降频避险）"},
        {"key": "regime_ma_short", "frozen": True, "label": "短均线", "type": "int", "default": 20,
         "min": 5, "max": 60, "group": "市场状态", "advanced": True,
         "description": "趋势判定短均线周期"},
        {"key": "regime_ma_long", "frozen": True, "label": "长均线", "type": "int", "default": 60,
         "min": 20, "max": 200, "group": "市场状态", "advanced": True,
         "description": "趋势判定长均线周期"},
        {"key": "regime_slope_n", "frozen": True, "label": "斜率窗口", "type": "int", "default": 5,
         "min": 2, "max": 20, "group": "市场状态", "advanced": True,
         "description": "均线近 N 日斜率方向判定"},
    ]

    def prepare(self, data: dict[str, pl.DataFrame], params: dict,
                start_date: str | None = None,
                market_regime: pl.DataFrame | None = None) -> dict[str, pl.DataFrame]:
        p = {k["key"]: k["default"] for k in self.param_schema}
        p.update({k: v for k, v in (params or {}).items() if v is not None})

        # 方案A：市场状态三态映射（day -> regime，由 runner 注入指数日线计算）
        regime_map: dict[str, str] = {}
        if market_regime is not None and market_regime.height:
            regime_map = {r[0]: r[1] for r in market_regime.rows()}

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
        # 2.5 池级趋势开关（POOL_GATE）：健康度=池内动量分>0 占比，
        #  滞回双阈值（触发 enter_th / 恢复 2×enter_th，确认 2 日，T-1 对齐）；
        #  关闭时全 False，行为与旧版一致
        if p.get("pool_gate"):
            gate_df = mc.pool_gate_column(feats, float(p.get("pool_gate_enter_th") or 0.15))
        else:
            gate_df = None
        # 3. 每股状态机
        out: dict[str, pl.DataFrame] = {}
        for code, df in data.items():
            df = df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
            # A1 点时可得性对齐（防未来函数）：特征整体后移一个交易日，
            # 当日 bar 只能"看见"上一完整交易日的特征，避免 09:35 就知道当日收盘。
            feats_t1 = feats[code].with_columns(
                pl.col("day").shift(-1).alias("day")).drop_nulls("day")
            df = df.join(feats_t1, on="day", how="left")
            if gate_df is not None:
                df = df.join(gate_df, on="day", how="left").with_columns(
                    pl.col("pool_gate").fill_null(False))
            else:
                df = df.with_columns(pl.lit(False).alias("pool_gate"))
            cols = self._walk(df, p, top_days.get(code, set()), start_date, regime_map)
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
              start_date: str | None,
              regime_map: dict[str, str] | None = None) -> list[pl.Series]:
        """生成 signal/tag/reason/budget_pct/t_ratio/reduce_pct 列。

        逐bar判定逻辑全部在 SlotStepper（本文件下方）：回测批量走完与
        实盘盘中"喂一根 bar 走一步"共用同一实现（单一事实来源，见
        LIVE_SIGNAL_SYSTEM §3 状态机步进化）。"""
        n = df.height
        signals = [0] * n
        tags = [""] * n
        reasons = [""] * n
        budgets: list[float | None] = [None] * n
        t_ratios: list[float | None] = [None] * n
        reduces: list[float | None] = [None] * n

        cols = ["date", "close", "atr_pct", "bias", "vol_pos", "breakout",
                "dif", "dea", "ma_fast", "slope", "score", "day_idx", "pool_gate"]
        # pool_gate 由 prepare 注入（POOL_GATE）；直调 _walk 的旧路径兜底补列
        if "pool_gate" not in df.columns:
            df = df.with_columns(pl.lit(False).alias("pool_gate"))
        dts = df["date"].to_list()
        is_eod = [i == n - 1 or dts[i][:10] != dts[i + 1][:10] for i in range(n)]
        st = SlotStepper(p, top_days, regime_map=regime_map)

        for i, row in enumerate(df.select(cols).iter_rows()):
            (date, close, atr_pct, bias, vol_pos, breakout,
             dif, dea, ma_fast, slope, score, day_idx, pool_gate) = row
            if start_date and date[:10] < start_date:
                continue
            sig = st.step(date, close, atr_pct, bias, vol_pos, breakout,
                          dif, dea, ma_fast, slope, score, day_idx,
                          bool(pool_gate), is_eod[i])
            if sig is not None:
                signals[i] = sig["signal"]
                tags[i] = sig["tag"]
                reasons[i] = sig["reason"]
                budgets[i] = sig.get("budget_pct")
                t_ratios[i] = sig.get("t_ratio")
                reduces[i] = sig.get("reduce_pct")

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
        - 一次性把所有 bar 按交易日归组（day -> [(code_idx, bar_idx)]），
          避免每天对每只股票全量扫描所有 bar。旧实现为 O(天数×股票数×bar数)
          （200只×20个月约 30 亿次 Python 迭代，是回测"加载数据"阶段最大瓶颈）；
          现为 O(总bar数)，毫秒级完成，判定语义完全不变。
        - 按时间顺序推进，维护当前持仓数(open_count)和每只股票的持仓状态
        - 当日有建仓信号(open=1)且 open_count >= max_holdings 时置0
          （释放槽位给更高分的候选股）
        - 退出信号(open=-1)释放槽位；做T高抛/买回不参与进出场判定

        注意：此为策略层近似实现，引擎风控(max_holdings)仍为最终屏障。
        """
        if max_holdings <= 0 or not data:
            return data

        codes = list(data)
        # 每只股票的 signal/tag/score 列（按 row order），按 code 索引取值
        code_signals: list[list[int]] = []
        code_tags: list[list[str]] = []
        code_scores: list[list[float]] = []
        # 按日归组：day -> [(code_idx, bar_idx)]（仅建一次）
        day_bars: dict[str, list[tuple[int, int]]] = {}
        for ci, code in enumerate(codes):
            df = data[code]
            sigs = df["signal"].to_list()
            tags = df["tag"].to_list()
            scores = df["score"].to_list()
            days = df["date"].str.slice(0, 10).to_list()
            code_signals.append(sigs)
            code_tags.append(tags)
            code_scores.append(scores)
            for i, d in enumerate(days):
                day_bars.setdefault(d, []).append((ci, i))

        held: dict[str, bool] = {}
        open_count = 0

        for day in sorted(day_bars):
            bars_today = day_bars[day]
            # 先处理退出信号（释放槽位）。
            # 做T高抛(sig=-1,tag=做T)只是同持仓内部减筹码，不释放槽位。
            for ci, i in bars_today:
                if (code_signals[ci][i] == -1 and code_tags[ci][i] != "做T"
                        and held.get(codes[ci], False)):
                    held[codes[ci]] = False
                    open_count = max(0, open_count - 1)

            # 再处理建仓信号（检查槽位，按score降序）。
            # 做T买回(sig=1,tag=做T)是回补已有持仓的债务，不占新槽位、不参与竞争，
            # 绝不因槽位已满被置零（否则高抛后永远买不回来，债务拖到期末）。
            entries = []
            for ci, i in bars_today:
                if (code_signals[ci][i] == 1 and code_tags[ci][i] != "做T"
                        and not held.get(codes[ci], False)):
                    sc = code_scores[ci][i]
                    if sc is None:
                        continue  # 跳过无score的条目（如崩溃保护期）
                    entries.append((-sc, ci, i))  # 负分用于升序排序
            entries.sort()
            for neg_score, ci, idx in entries:
                code = codes[ci]
                if open_count >= max_holdings:
                    # 槽位已满：将此code当日全部signal置0
                    data[code] = data[code].with_columns(
                        pl.when(pl.col("date").str.slice(0, 10) == day)
                          .then(0)
                          .otherwise(pl.col("signal"))
                          .alias("signal")
                    )
                else:
                    open_count += 1
                    held[code] = True

        return data


class SlotStepper:
    """momentum_slot 逐bar状态机步进器（回测 _walk 与实盘盘中信号机共用）。

    step() 喂一根 bar：close 为当前 bar 收盘价，其余为 T-1 对齐的日线特征
    （atr_pct/bias/vol_pos/breakout/dif/dea/ma_fast/slope/score/day_idx），
    返回信号 dict（signal/tag/reason/budget_pct/t_ratio/reduce_pct）或 None。
    状态保留在实例内：回测一次跑完；实盘每日经 state()/restore() 落库恢复。
    信号优先级（从高到低）：
      0) ATR硬止损（盘中实时，最高优先级）
      1) 衰退初期退出（渐进式：首次减partial_exit_pct->二次清仓）
      2) 加速启动建仓（金叉+站上快均线+候选池+冷却期）
      3) 试仓升级（斜率确认->满配）
      4) 金字塔加仓（突破新高+冷却+递减）
      5) 做T（正向T逢低买入 / 反向T高抛低吸）
    """

    def __init__(self, p: dict, top_days: set, regime_map: dict[str, str] | None = None):
        self.base_min = float(p["base_pct_min"])
        self.base_max = float(p["base_pct_max"])
        self._g_mult = float(p["grid_atr_mult"])
        self._g_floor = float(p["grid_floor_pct"]) / 100.0
        self._g_asym = float(p["asym_bias"])
        self.t_base = float(p["t_ratio_base"]) / 100.0
        self.max_t = int(p["max_t_times"])
        self.t_mode = str(p.get("t_mode") or "grid")
        self.asym_sell_cap = float(p.get("asym_sell_cap") or 2.0)
        if self.t_mode == "off":
            self.max_t = 0
        self.vol_grid_hi = float(p["vol_grid_hi"])
        self.vol_grid_lo = float(p["vol_grid_lo"])
        self.t_vol_hi = float(p["t_vol_hi"])
        self.t_vol_lo = float(p["t_vol_lo"])
        self.t_decay = float(p["t_decay"])
        self.max_adds = int(p["max_adds"])
        self.add_scale = float(p["add_scale"])
        self.add_cd = int(p["add_cooldown"])
        self.exit_need = int(p["exit_need"])
        self.exit_cd = int(p["exit_cooldown"])
        self.atr_stop_k = float(p.get("atr_stop_k") or -3.0)
        self.decay_pct = float(p.get("decay_pct") or 0.15)
        self.partial_exit_pct = float(p.get("partial_exit_pct") or 50)
        # 优化：二清确认期 / 跌出榜单事件化（0=关闭，保持原行为，A/B 基线不变）
        self.exit_confirm_days = int(p.get("exit_confirm_days") or 0)
        self.out_top_days = int(p.get("out_top_days") or 0)
        # 方案D：动量状态机（off=沿用现有衰退信号）
        self.momentum_fsm_on = str(p.get("momentum_fsm_on") or "off") == "on"
        self.exit_fade_days = int(p.get("exit_fade_days") or 2)
        # 方案A：市场状态三态（trend/range/crash）——核心仓预算与做T频率按环境缩放
        self.market_regime_on = str(p.get("market_regime_on") or "off") == "on"
        self.regime_map = regime_map or {}
        self.core_scale_range = float(p.get("core_scale_range") or 1.0)
        self.t_scale_range = float(p.get("t_scale_range") or 1.0)
        self.core_scale_crash = float(p.get("core_scale_crash") or 1.0)
        self.t_scale_crash = float(p.get("t_scale_crash") or 1.0)
        self.fwd_t = str(p.get("fwd_t") or "off")
        self.fwd_t_budget = float(p.get("fwd_t_budget_pct") or 25) / 100.0
        self.trend_clock = str(p.get("trend_clock") or "intraday")
        self.ma_fast_n = int(p.get("ma_fast") or 20)
        self.add_breakout_n = int(p.get("add_breakout_n") or 20)
        self.top_days = top_days
        # ---- 状态（与旧 _walk 局部变量一一对应） ----
        self.opened = False
        self.full = False
        self.adds_done = 0
        self.last_add_idx = -10**9
        self.last_exit_idx = -10**9
        # P1 防同价：开仓以来最高收盘。加仓要求当前价高于它（新高须发生在
        # 建仓之后，而非入选时已成立的存量 20 日新高），消灭开仓即加仓
        self.high_since_open: float | None = None
        self.cur_day: str | None = None
        self.ref: float | None = None
        self.t_count = 0
        # 动量衰减跟踪：持仓期间score峰值
        self.score_peak: float | None = None
        # 渐进式退出阶段: 0=无退出, 1=已部分减仓待清仓, 2=已清仓
        self.exit_stage = 0
        # 优化：二清确认期状态（首减日 / 上一bar MACD金叉态判新死叉）
        self.first_reduce_idx = -10**9
        self.macd_was_ok = False
        self.has_reduced = False  # 本次持仓是否已首减过（取消二清后不再重复首减）
        # 优化：跌出榜单事件化状态（日级连续性）
        self.out_top_count = 0
        self.out_top_fired = False
        # 方案D：动量状态机状态（mom_state=idle/cruise/decel/fade；fade_streak=连续衰竭交易日）
        self.mom_state = "idle"
        self.fade_streak = 0
        self.fade_today = False

    # ---------------- 方案A：市场状态（trend/range/crash）缩放 ----------------

    def _regime(self, date: str) -> str:
        """当日市场状态；开关关 或 映射缺失时一律 trend（不缩放，兼容现状）。"""
        if self.market_regime_on and self.regime_map:
            return self.regime_map.get(date[:10], "trend")
        return "trend"

    def _t_cap(self, date: str) -> int:
        """当日做T次数上限（日内 T 频率按市场状态缩放，至少 1 次）。"""
        if not self.market_regime_on:
            return self.max_t
        r = self._regime(date)
        scale = self.t_scale_crash if r == "crash" else (self.t_scale_range if r == "range" else 1.0)
        return max(1, int(round(self.max_t * scale)))

    def _core_scale(self, date: str) -> float:
        """当日核心仓预算系数（震荡/防守收缩核心仓）。"""
        if not self.market_regime_on:
            return 1.0
        r = self._regime(date)
        if r == "crash":
            return self.core_scale_crash
        if r == "range":
            return self.core_scale_range
        return 1.0

    def state(self) -> dict:
        """导出状态（实盘 sig_strategy_state 落库 / 跨日恢复）"""
        return {"opened": int(self.opened), "full": int(self.full),
                "adds_done": self.adds_done,
                "last_add_idx": self.last_add_idx,
                "last_exit_idx": self.last_exit_idx,
                "exit_stage": self.exit_stage,
                "first_reduce_idx": self.first_reduce_idx,
                "macd_was_ok": int(self.macd_was_ok),
                "has_reduced": int(self.has_reduced),
                "out_top_count": self.out_top_count,
                "out_top_fired": int(self.out_top_fired),
                "mom_state": self.mom_state,
                "fade_streak": self.fade_streak,
                "fade_today": int(self.fade_today),
                "high_since_open": self.high_since_open,
                "ref": self.ref, "t_count": self.t_count,
                "score_peak": self.score_peak, "cur_day": self.cur_day}

    def restore(self, st: dict) -> None:
        """从落库快照恢复状态（缺键保持默认）"""
        if not st:
            return
        self.opened = bool(st.get("opened"))
        self.full = bool(st.get("full"))
        self.adds_done = int(st.get("adds_done") or 0)
        self.last_add_idx = int(st.get("last_add_idx") if st.get("last_add_idx") is not None else -10**9)
        self.last_exit_idx = int(st.get("last_exit_idx") if st.get("last_exit_idx") is not None else -10**9)
        self.exit_stage = int(st.get("exit_stage") or 0)
        self.first_reduce_idx = int(st.get("first_reduce_idx") if st.get("first_reduce_idx") is not None else -10**9)
        self.macd_was_ok = bool(st.get("macd_was_ok"))
        self.has_reduced = bool(st.get("has_reduced"))
        self.out_top_count = int(st.get("out_top_count") or 0)
        self.out_top_fired = bool(st.get("out_top_fired"))
        self.mom_state = str(st.get("mom_state") or "idle")
        self.fade_streak = int(st.get("fade_streak") or 0)
        self.fade_today = bool(st.get("fade_today"))
        self.high_since_open = st.get("high_since_open")
        self.ref = st.get("ref")
        self.t_count = int(st.get("t_count") or 0)
        self.score_peak = st.get("score_peak")
        self.cur_day = st.get("cur_day")

    def step(self, date: str, close: float, atr_pct, bias, vol_pos, breakout,
             dif, dea, ma_fast, slope, score, day_idx, pool_gate: bool,
             is_eod: bool) -> dict | None:
        """喂一根 bar，返回信号 dict 或 None（is_eod：当日末 bar，daily 时钟用）"""
        day = date[:10]
        if day != self.cur_day:
            self.cur_day = day
            self.ref = None
            self.t_count = 0
            self.score_peak = None
            # 优化：跌出榜单连续性（日级事件化，out_top_days>0 时启用）
            if self.out_top_days > 0:
                if day not in self.top_days:
                    self.out_top_count += 1
                else:
                    self.out_top_count = 0
                    self.out_top_fired = False
            # 方案D：结转昨日衰竭连续性（跨交易日）
            if self.fade_today:
                self.fade_streak += 1
            else:
                self.fade_streak = 0
            self.fade_today = False
        trend_ok = (self.trend_clock != "daily") or is_eod

        # P1：滚动维护开仓以来最高收盘。prev_high=截至上一根 bar 的最高
        # （加仓检查用它——当前 bar 刚创的新高本身不作为自己的加仓依据）
        prev_high = self.high_since_open
        if self.opened and self.high_since_open is not None and close > self.high_since_open:
            self.high_since_open = close

        macd_ok = dif is not None and dea is not None and dif > dea
        above_fast = ma_fast is not None and close > ma_fast
        slope_up = slope is not None and slope > 0
        # 衰退初期三信号（个股级，T-1 特征，次日成交）
        s1 = dif is not None and dea is not None and dif < dea
        s2 = ma_fast is not None and close < ma_fast
        # 动量衰减：持仓期间score从峰值回落超过decay_pct
        if self.opened and score is not None:
            if self.score_peak is None or score > self.score_peak:
                self.score_peak = score
            score_decay = (self.score_peak is not None
                           and score < self.score_peak * (1 - self.decay_pct))
        else:
            score_decay = False
        # 优化：跌出榜单事件化——out_top_days>0 时连续 N 日不在榜才计一次，不再每天重复触发
        if self.out_top_days > 0:
            out_trigger = self.out_top_count >= self.out_top_days and not self.out_top_fired
            if out_trigger:
                self.out_top_fired = True  # 本次跌出序列只计一次，重新入榜时由 cur_day 分支重置
        else:
            out_trigger = day not in self.top_days  # 原行为：每天不在榜即信号
        s3 = ((score is not None and score < 0)
              or out_trigger
              or score_decay)

        # 方案D：动量状态机（momentum_fsm_on 时用 减速/衰竭 替代「3信号凑数」判定退出）
        if self.momentum_fsm_on:
            fsm_decel = score_decay  # 减速：score 从峰值回落超 decay_pct（仅状态标记）
            # 完整新设计（V2）：fade 仅在日线收盘（EOD）用收盘价确认。
            # V1 实测证伪：盘中 5 分钟跌破MA20/score<0 太灵敏，插针即触发过度退出；
            # 改为日线收盘确认后，仅当当日收盘真正转弱才触发退出，5 分钟插针被过滤。
            fsm_fade = is_eod and ((score is not None and score < 0)
                                   or (ma_fast is not None and close < ma_fast))
            self.fade_today = fsm_fade
            self.mom_state = "fade" if fsm_fade else ("decel" if fsm_decel else "cruise")
            # V1 证伪：decel 不单独触发退出（动量正常回落几乎总伴随衰竭，去掉不影响）
            exit_trigger = fsm_fade
        else:
            self.fade_today = False
            self.mom_state = "idle"
            exit_trigger = (int(s1) + int(s2) + int(s3)) >= self.exit_need

        # ---- 0) ATR硬止损（最高优先级，盘中实时触发） ----
        if self.opened and bias is not None and bias < self.atr_stop_k:
            self.opened, self.full, self.adds_done = False, False, 0
            self.exit_stage = 0
            self.has_reduced = False
            return {"signal": -1, "tag": "止损",
                    "reason": (f"ATR硬止损(bias={bias:.1f}<-{self.atr_stop_k})"
                               f" 价格跌破{self.atr_stop_k}倍ATR")}

        # ---- 1) 衰退初期退出：渐进式（首次减仓->二次清仓） ----
        if self.opened and trend_ok and exit_trigger:
            if self.momentum_fsm_on:
                # 状态机 hits：减速/衰竭
                hits = []
                if fsm_decel: hits.append("动量减速")
                if fsm_fade:
                    hits.append("动量衰竭" if (score is not None and score < 0)
                                else f"跌破MA{self.ma_fast_n}")
            else:
                hits = []
                if s1: hits.append("MACD死叉")
                if s2: hits.append(f"跌破MA{self.ma_fast_n}")
                if score_decay: hits.append("动量衰减")
                if score is not None and score < 0: hits.append("动量转负")
                if day not in self.top_days: hits.append("跌出榜单")
            if self.exit_stage == 0:
                if self.has_reduced:
                    # 已首减过（此前取消过二清）：信号再次满足 -> 直接清仓，不再重复首减
                    self.opened, self.full, self.adds_done = False, False, 0
                    self.exit_stage = 2
                    self.last_exit_idx = day_idx
                    self.has_reduced = False
                    return {"signal": -1, "tag": "",
                            "reason": f"衰退清仓(二次): {'+'.join(hits)}"}
                self.has_reduced = True
                self.exit_stage = 1
                self.first_reduce_idx = day_idx
                self.macd_was_ok = bool(macd_ok)
                return {"signal": -1, "tag": "减仓",
                        "reduce_pct": self.partial_exit_pct,
                        "reason": (f"衰退初期(首次减{self.partial_exit_pct:.0f}%): "
                                   f"{'+'.join(hits)}")}
            if self.exit_stage == 1:
                if self.momentum_fsm_on:
                    # 二清（状态机）：衰竭连续确认 exit_fade_days 天 -> 立即二清
                    fade_n = self.fade_streak + (1 if fsm_fade else 0)
                    if fsm_fade and self.exit_fade_days > 0 and fade_n >= self.exit_fade_days:
                        self.opened, self.full, self.adds_done = False, False, 0
                        self.exit_stage = 2
                        self.last_exit_idx = day_idx
                        self.has_reduced = False
                        return {"signal": -1, "tag": "",
                                "reason": f"衰退清仓(衰竭{fade_n}日): {'+'.join(hits)}"}
                    # 新死叉（急跌）-> 立即二清
                    if (not macd_ok) and self.macd_was_ok:
                        self.opened, self.full, self.adds_done = False, False, 0
                        self.exit_stage = 2
                        self.last_exit_idx = day_idx
                        self.has_reduced = False
                        return {"signal": -1, "tag": "",
                                "reason": f"衰退确认(新死叉): {'+'.join(hits)}"}
                    # 收复快均线且重新入榜 -> 取消二清，恢复半仓持有
                    if close > ma_fast and day in self.top_days:
                        self.exit_stage = 0
                        return None
                    return None  # 待衰竭确认 / 恢复
                # ---- 原（非状态机）二清：确认期三出口 ----
                if self.exit_confirm_days > 0 and (day_idx - self.first_reduce_idx) < self.exit_confirm_days:
                    # 出口2：观察期内出现新死叉（急跌）-> 立即二清
                    if (not macd_ok) and self.macd_was_ok:
                        self.opened, self.full, self.adds_done = False, False, 0
                        self.exit_stage = 2
                        self.last_exit_idx = day_idx
                        self.has_reduced = False
                        return {"signal": -1, "tag": "",
                                "reason": f"衰退确认(新死叉): {'+'.join(hits)}"}
                    # 出口1：观察期内收复快均线且重新入榜 -> 取消二清，恢复半仓持有
                    if close > ma_fast and day in self.top_days:
                        self.exit_stage = 0
                        return None
                    return None  # 观察期内待定（不加仓/不做T，等待出口）
                self.opened, self.full, self.adds_done = False, False, 0
                self.exit_stage = 2
                self.last_exit_idx = day_idx
                self.has_reduced = False
                return {"signal": -1, "tag": "",
                        "reason": f"衰退清仓(二次): {'+'.join(hits)}"}
        elif self.exit_stage == 1 and not self.momentum_fsm_on and self.exit_confirm_days > 0 and not exit_trigger:
            # 原逻辑：待二清但衰退信号已不满足（去持续性后可能出现）-> 取消二清，恢复持有
            self.exit_stage = 0
            return None
        elif self.exit_stage == 1 and self.momentum_fsm_on and not exit_trigger:
            # 状态机：减速/衰竭均不满足 -> 取消二清，恢复持有
            self.exit_stage = 0
            return None

        if not self.opened:
            # ---- 2) 加速启动建仓（池级开关 pool_gate 抑制：POOL_GATE）----
            if (macd_ok and above_fast and day in self.top_days
                    and (day_idx - self.last_exit_idx) >= self.exit_cd and trend_ok
                    and not pool_gate):
                if slope_up:
                    self.opened, self.full = True, True
                    out = {"signal": 1, "tag": "开仓", "budget_pct": self.base_max * self._core_scale(date),
                           "reason": "加速启动(金叉+站上快均线+入榜+斜率向上)，满配建仓"}
                else:
                    self.opened, self.full = True, False
                    out = {"signal": 1, "tag": "开仓", "budget_pct": self.base_min * self._core_scale(date),
                           "reason": "加速启动(金叉+站上快均线+入榜)，试仓建仓"}
                # P1：冷却期自开仓日起算；新高基准 = 开仓bar收盘
                self.high_since_open = close
                self.last_add_idx = day_idx
                self.has_reduced = False  # 新持仓周期：重置退出状态
                self.fade_streak = 0      # 方案D：重置衰竭连续性
                self.mom_state = "cruise"
                return out
            return None

        # ---- 3) 试仓升级 ----
        if not self.full and slope_up and trend_ok and not pool_gate:
            self.full = True
            return {"signal": 1, "tag": "加仓",
                    "budget_pct": max(0.0, self.base_max - self.base_min) * self._core_scale(date),
                    "reason": "斜率确认，试仓升级满配"}

        # ---- 4) 金字塔加仓：突破新高 + 冷却期 + 次数递减 ----
        # P1 防同价：冷却期自开仓日起算，且要求当前价高于开仓以来的
        # 最高收盘（新高须发生在建仓之后，而非入选时已成立的存量新高）；
        # P2 最小有效量：预算不足总资产 ADD_MIN_BUDGET_PCT% 时跳过，
        # 不消耗加仓次数与冷却期（防低 base_max 下金字塔衰减为无意义小单）
        if (self.full and breakout and self.adds_done < self.max_adds
                and (day_idx - self.last_add_idx) >= self.add_cd and trend_ok
                and not pool_gate
                and prev_high is not None and close > prev_high):
            budget = self.base_max * (self.add_scale ** (self.adds_done + 1))
            if budget >= mc.ADD_MIN_BUDGET_PCT:
                nth = self.adds_done + 1
                self.adds_done = nth
                self.last_add_idx = day_idx
                self.ref = close
                return {"signal": 1, "tag": "加仓", "budget_pct": budget,
                        "reason": (f"突破{self.add_breakout_n}日新高，"
                                   f"第{nth}次金字塔加仓")}

        # ---- 5) 做T ----
        if self.t_mode == "time":
            if " " not in date or self.t_count >= self._t_cap(date):
                return None
            hhmm = date[11:16]
            if hhmm == "09:35":
                self.t_count += 1
                return {"signal": -1, "tag": "做T", "t_ratio": 25.0,
                        "reason": "时点T：09:35高抛1/4底仓"}
            if hhmm == "14:50":
                self.t_count += 1
                return {"signal": 1, "tag": "做T", "budget_pct": self.base_min,
                        "reason": "时点T：14:50尾盘买回"}
            return None
        if atr_pct is None or self.t_count >= self._t_cap(date):
            return None
        g = float(atr_pct) * self._g_mult
        if g <= 0:
            return None
        vp = vol_pos if vol_pos is not None else 0.5
        g *= self.vol_grid_lo + (self.vol_grid_hi - self.vol_grid_lo) * vp
        g = max(g, self._g_floor)
        b = bias if bias is not None else 0.0
        g_sell = g * (1 + self._g_asym) if b > 0 else g * (1 - self._g_asym)
        g_buy = g * (1 - self._g_asym) if b > 0 else g * (1 + self._g_asym)
        vol_mult = self.t_vol_lo + (self.t_vol_hi - self.t_vol_lo) * vp
        ratio = min(1.0, self.t_base * vol_mult * (self.t_decay ** self.t_count))

        if self.ref is None:
            self.ref = close
            return None

        # ---- 正向T：逢低买入（池级开关 pool_gate 抑制：下跌市接飞刀）----
        if (self.fwd_t == "on" and not pool_gate
                and close <= self.ref * (1 - g_buy) and self.opened):
            self.ref = close
            self.t_count += 1
            return {"signal": 1, "tag": "做T", "t_ratio": ratio * 100,
                    "budget_pct": self.fwd_t_budget * 100,
                    "reason": (f"正向T：跌破下网格线(阈值{g_buy * 100:.2f}%)"
                               f"逢低买入(预算{self.fwd_t_budget*100:.0f}%)")}

        # ---- 反向T ----
        if close <= self.ref * (1 - g_buy):
            self.ref = close
            self.t_count += 1
            return {"signal": 1, "tag": "做T", "t_ratio": ratio * 100,
                    "budget_pct": self.base_min,
                    "reason": f"跌破下网格线(阈值{g_buy * 100:.2f}%)买回"}
        if b <= self.asym_sell_cap and close >= self.ref * (1 + g_sell):
            self.ref = close
            self.t_count += 1
            return {"signal": -1, "tag": "做T", "t_ratio": ratio * 100,
                    "reason": f"升破上网格线(阈值{g_sell * 100:.2f}%)高抛"}
        return None
