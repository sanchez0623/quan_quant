# -*- coding: utf-8 -*-
"""分层持仓模型：Position（建仓组 group_id + 开仓类型 tag）与账户 Portfolio"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    code: str
    volume: int
    cost_price: float          # 后复权成本（加权平均，加仓会抬高）
    open_time: str             # 开仓bar时间
    sellable_date: Optional[str]  # T+1: 可卖的最早交易日
    group_id: int              # 建仓组：同组开仓+后续加仓
    tag: str                   # 开仓类型标签: 开仓/加仓/做T
    highest_price: float = 0.0  # 移动止损用（后复权）
    open_fee: float = 0.0
    # ATR_TRAILING：首笔开仓价（不随加仓改写）。金字塔加仓会抬高加权成本，
    # 若止损线锚定加权成本，加仓后小幅回调即触发「假止损」；首笔价提供稳定基准。
    first_price: float = 0.0
    # ATR_TRAILING：当前生效的移动止损线，只上不下（ratchet），避免回调时止损线回落
    trail_stop: float = 0.0

    def __post_init__(self) -> None:
        if self.highest_price == 0.0:
            self.highest_price = self.cost_price
        if self.first_price == 0.0:
            self.first_price = self.cost_price


class Portfolio:
    """现金 + 分层持仓账户（价格为后复权口径）"""

    def __init__(self, cash: float):
        self.initial_cash = float(cash)
        self.cash = float(cash)
        self.positions: list[Position] = []
        self._group_seq = 0

    def next_group_id(self) -> int:
        self._group_seq += 1
        return self._group_seq

    def positions_of(self, code: str) -> list[Position]:
        return [p for p in self.positions if p.code == code]

    def volume_of(self, code: str) -> int:
        return sum(p.volume for p in self.positions if p.code == code)

    def market_value(self, price_map: dict[str, float]) -> float:
        """price_map: code -> 最新收盘价（后复权）"""
        return sum(p.volume * price_map.get(p.code, p.cost_price) for p in self.positions)

    def equity(self, price_map: dict[str, float]) -> float:
        return self.cash + self.market_value(price_map)

    def add_position(self, code: str, volume: int, price: float, open_time: str,
                     sellable_date: Optional[str], tag: str, fee: float,
                     group_id: Optional[int] = None) -> Position:
        # 加权摊薄成本（同组）或新组成本
        if group_id is not None:
            same = [p for p in self.positions if p.code == code and p.group_id == group_id]
            if same:
                base = same[0]
                total_vol = base.volume + volume
                base.cost_price = (base.cost_price * base.volume + price * volume) / total_vol
                base.volume = total_vol
                base.open_fee += fee
                base.highest_price = max(base.highest_price, price)
                return base
        pos = Position(code=code, volume=volume, cost_price=price, open_time=open_time,
                       sellable_date=sellable_date, group_id=group_id or self.next_group_id(),
                       tag=tag, open_fee=fee, highest_price=price)
        self.positions.append(pos)
        return pos
