# -*- coding: utf-8 -*-
"""撮合规则（A股真实约束）：T+1、涨跌停一字板、滑点、手续费、100股整数倍"""
from typing import Optional


class Broker:
    def __init__(self, slippage_pct: float = 0.001, commission_rate: float = 0.0003,
                 commission_min: float = 5.0, stamp_tax: float = 0.001,
                 transfer_fee: float = 0.00001):
        self.slippage_pct = float(slippage_pct)
        self.commission_rate = float(commission_rate)
        self.commission_min = float(commission_min)
        self.stamp_tax = float(stamp_tax)
        self.transfer_fee = float(transfer_fee)

    # ---------------- 手续费 ----------------

    def buy_fee(self, amount: float) -> float:
        """买单：佣金（有最低）+ 过户费"""
        return round(max(amount * self.commission_rate, self.commission_min)
                     + amount * self.transfer_fee, 2)

    def sell_fee(self, amount: float) -> float:
        """卖单：佣金（有最低）+ 印花税 + 过户费"""
        return round(max(amount * self.commission_rate, self.commission_min)
                     + amount * self.stamp_tax + amount * self.transfer_fee, 2)

    # ---------------- 价格 ----------------

    def buy_price(self, open_price: float) -> float:
        return open_price * (1 + self.slippage_pct)

    def sell_price(self, open_price: float) -> float:
        return open_price * (1 - self.slippage_pct)

    # ---------------- 涨跌停（一字板近似判定） ----------------

    @staticmethod
    def is_limit_up(bar: dict, prev_close: Optional[float]) -> bool:
        """一字涨停：high==low 且 close>prev_close → 买不进"""
        if prev_close is None:
            return False
        return bar["high"] == bar["low"] and bar["close"] > prev_close

    @staticmethod
    def is_limit_down(bar: dict, prev_close: Optional[float]) -> bool:
        """一字跌停：high==low 且 close<prev_close → 卖不出"""
        if prev_close is None:
            return False
        return bar["high"] == bar["low"] and bar["close"] < prev_close

    # ---------------- 数量 ----------------

    @staticmethod
    def lots_for_amount(amount: float, price: float) -> int:
        """按金额计算 100 股整数倍数量"""
        if price <= 0 or amount < price * 100:
            return 0
        return int(amount / price // 100) * 100
