# -*- coding: utf-8 -*-
"""撮合规则（A股真实约束）：T+1、涨跌停（分板块/ST 差异化涨跌幅）、
成交量参与率约束、市场冲击滑点、手续费、100股整数倍。

费用结构（2026年现行）：佣金（双边，最低5元）+ 印花税（仅卖出万5）
+ 经手费（双边万0.341）+ 证管费（双边万0.2）+ 过户费（双边万0.1）
"""
from typing import Optional


class Broker:
    def __init__(self, slippage_pct: float = 0.001, commission_rate: float = 0.00005,
                 commission_min: float = 5.0, stamp_tax: float = 0.0005,
                 transfer_fee: float = 0.00001, handling_fee: float = 0.0000341,
                 regulatory_fee: float = 0.00002,
                 volume_participation: float = 0.1, impact_k: float = 0.1):
        """
        volume_participation: 单笔订单最大可吃当 bar 成交量的比例（0=不限制）。
            个人小资金（百万以内）默认 10% 通常不触发；资金量增大或小票流动性不足时
            会自动缩量，避免“open 价无限成交”的不现实假设。
        impact_k: 市场冲击系数，实际滑点 = slippage_pct * (1 + impact_k * 订单量/当bar量)。
            0 关闭冲击，退化为固定比例滑点。
        """
        self.slippage_pct = float(slippage_pct)
        self.commission_rate = float(commission_rate)
        self.commission_min = float(commission_min)
        self.stamp_tax = float(stamp_tax)
        self.transfer_fee = float(transfer_fee)
        self.handling_fee = float(handling_fee)      # 经手费（双边）
        self.regulatory_fee = float(regulatory_fee)  # 证管费（双边）
        self.volume_participation = float(volume_participation)
        self.impact_k = float(impact_k)

    # ---------------- 手续费 ----------------

    def _bilateral_fee(self, amount: float) -> float:
        """双边固定比例费用：经手费 + 证管费 + 过户费"""
        return amount * (self.handling_fee + self.regulatory_fee + self.transfer_fee)

    def buy_fee(self, amount: float) -> float:
        """买单：佣金（有最低）+ 经手费 + 证管费 + 过户费"""
        return round(max(amount * self.commission_rate, self.commission_min)
                     + self._bilateral_fee(amount), 2)

    def sell_fee(self, amount: float) -> float:
        """卖单：佣金（有最低）+ 印花税 + 经手费 + 证管费 + 过户费"""
        return round(max(amount * self.commission_rate, self.commission_min)
                     + amount * self.stamp_tax + self._bilateral_fee(amount), 2)

    # ---------------- 价格（含市场冲击滑点） ----------------

    def _impact_slip(self, bar_volume: float, order_volume: float) -> float:
        """订单相对当 bar 成交量越大，滑点越高；无量或关闭冲击时返回基础滑点。"""
        if self.impact_k <= 0 or bar_volume <= 0 or order_volume <= 0:
            return self.slippage_pct
        ratio = min(1.0, order_volume / bar_volume)
        return self.slippage_pct * (1.0 + self.impact_k * ratio)

    def buy_price(self, open_price: float, bar_volume: float = 0.0,
                  order_volume: float = 0.0) -> float:
        return open_price * (1 + self._impact_slip(bar_volume, order_volume))

    def sell_price(self, open_price: float, bar_volume: float = 0.0,
                   order_volume: float = 0.0) -> float:
        return open_price * (1 - self._impact_slip(bar_volume, order_volume))

    # ---------------- 成交量约束 ----------------

    def cap_volume(self, want_volume: int, bar_volume: float) -> int:
        """按参与率上限缩量（向下取整到 100 股）。bar_volume<=0 或关闭参与率时不缩。"""
        if self.volume_participation <= 0 or bar_volume <= 0:
            return want_volume
        max_vol = int(bar_volume * self.volume_participation)
        max_vol = (max_vol // 100) * 100
        return max(0, min(want_volume, max_vol))

    # ---------------- 涨跌停（分板块/ST 差异化涨跌幅） ----------------

    @staticmethod
    def limit_pct(code: str, is_st: bool = False, date: Optional[str] = None) -> float:
        """返回当日涨跌幅限制比例（小数）。

        - ST/*ST：5%
        - 创业板(300/301)：2020-08-24 起 20%，之前 10%
        - 科创板(688/689)：20%
        - 北交所(4/8 开头)：30%
        - 主板(60/00)：10%
        上市首日无涨跌幅限制的情形需 list_date 支撑，此处不单独建模（回测窗口通常避开首日）。
        """
        c = str(code).strip()
        if is_st:
            return 0.05
        if c.startswith(("300", "301")):
            # 创业板注册制首批 2020-08-24 上市，此前为 10%
            if date and date < "2020-08-24":
                return 0.10
            return 0.20
        if c.startswith(("688", "689")):
            return 0.20
        if c.startswith(("4", "8")):
            return 0.30
        return 0.10

    @staticmethod
    def is_limit_up(bar: dict, prev_close: Optional[float],
                    limit_pct: float = 0.10) -> bool:
        """一字涨停：全天封死（open==high==low==close）且收在涨停价 -> 买不进。

        用实际涨停价（前日收盘 ×(1+涨跌幅)，四舍五入到分）判定，避免把
        普通低波动/停牌一字线误判为涨停。盘中开板（high≠low）视为可成交，
        不在此拦截（回测以开盘价成交，属于乐观假设，实盘需排队）。
        """
        if prev_close is None or prev_close <= 0:
            return False
        if bar["high"] != bar["low"]:
            return False  # 盘中开过板，不按一字板处理
        limit_up = round(prev_close * (1 + limit_pct) + 1e-9, 2)
        return abs(float(bar["close"]) - limit_up) <= 0.011

    @staticmethod
    def is_limit_down(bar: dict, prev_close: Optional[float],
                      limit_pct: float = 0.10) -> bool:
        """一字跌停：全天封死且收在跌停价 -> 卖不出。"""
        if prev_close is None or prev_close <= 0:
            return False
        if bar["high"] != bar["low"]:
            return False
        limit_down = round(prev_close * (1 - limit_pct) + 1e-9, 2)
        return abs(float(bar["close"]) - limit_down) <= 0.011

    # ---------------- 数量 ----------------

    @staticmethod
    def lots_for_amount(amount: float, price: float) -> int:
        """按金额计算 100 股整数倍数量"""
        if price <= 0 or amount < price * 100:
            return 0
        return int(amount / price // 100) * 100
