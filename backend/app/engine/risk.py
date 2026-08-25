# -*- coding: utf-8 -*-
"""风控模块（与策略参数分离）：仓位上限、回撤熔断、日内交易次数、止损/止盈/移动止损"""


class RiskConfig:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.max_position_pct_per_stock = float(cfg.get("max_position_pct_per_stock", 30))
        self.max_total_position_pct = float(cfg.get("max_total_position_pct", 100))
        self.stop_loss_mode = cfg.get("stop_loss_mode", "fixed")  # fixed | atr | trailing
        self.stop_loss_pct = float(cfg.get("stop_loss_pct", 8.0))
        self.atr_period = int(cfg.get("atr_period", 14))
        self.atr_multiplier = float(cfg.get("atr_multiplier", 2.0))
        self.take_profit_pct = float(cfg.get("take_profit_pct", 0) or 0)
        self.trailing_stop_pct = float(cfg.get("trailing_stop_pct", 0) or 0)
        self.max_drawdown_breaker = float(cfg.get("max_drawdown_breaker", 30))
        self.max_intraday_trades = int(cfg.get("max_intraday_trades", 4))


class RiskManager:
    """runner 在每bar撮合前调用检查"""

    def __init__(self, config: RiskConfig):
        self.cfg = config
        self.peak_equity: float = 0.0
        self.broken: bool = False  # 回撤熔断：停止开新仓

    # ---------------- 买入前检查 ----------------

    def update_equity(self, equity: float) -> None:
        """每bar更新净值，检查回撤熔断"""
        self.peak_equity = max(self.peak_equity, equity)
        if self.cfg.max_drawdown_breaker > 0 and self.peak_equity > 0:
            dd = 1 - equity / self.peak_equity
            if dd * 100 >= self.cfg.max_drawdown_breaker:
                self.broken = True

    def allow_buy(self, equity: float, total_market_value: float, code: str,
                  code_market_value: float, add_amount: float,
                  intraday_trades: int) -> tuple[bool, str]:
        """返回 (是否允许, 原因)"""
        if self.broken:
            return False, "回撤熔断"
        if equity <= 0:
            return False, "无可用资金"
        if intraday_trades >= self.cfg.max_intraday_trades:
            return False, "超出日内交易次数限制"
        # 个股仓位上限
        cap_stock = equity * self.cfg.max_position_pct_per_stock / 100
        if code_market_value + add_amount > cap_stock * 1.0001:
            add_amount = max(0.0, cap_stock - code_market_value)
            if add_amount <= 0:
                return False, "个股仓位已达上限"
        # 总仓位上限
        cap_total = equity * self.cfg.max_total_position_pct / 100
        room = cap_total - total_market_value
        if room <= 0:
            return False, "总仓位已达上限"
        return True, ""

    def buy_budget(self, equity: float, total_market_value: float, code: str,
                   code_market_value: float, cash: float) -> float:
        """计算允许的买入金额上限"""
        cap_stock = equity * self.cfg.max_position_pct_per_stock / 100
        by_stock = max(0.0, cap_stock - code_market_value)
        cap_total = equity * self.cfg.max_total_position_pct / 100
        by_total = max(0.0, cap_total - total_market_value)
        return max(0.0, min(by_stock, by_total, cash))

    # ---------------- 持仓中检查（返回 (action, reason)） ----------------

    def check_stop(self, pos, price: float, atr: float | None) -> tuple[str, str] | None:
        """返回 (动作: stop_loss|take_profit, reason) 或 None"""
        c = self.cfg
        if c.take_profit_pct > 0 and price >= pos.cost_price * (1 + c.take_profit_pct / 100):
            return "take_profit", f"止盈{c.take_profit_pct:g}%"
        if c.stop_loss_mode == "fixed":
            if price <= pos.cost_price * (1 - c.stop_loss_pct / 100):
                return "stop_loss", f"固定止损{c.stop_loss_pct:g}%"
        elif c.stop_loss_mode == "atr":
            if atr is not None and price <= pos.cost_price - atr * c.atr_multiplier:
                return "stop_loss", f"ATR止损x{c.atr_multiplier:g}"
        elif c.stop_loss_mode == "trailing":
            if c.trailing_stop_pct > 0 and price <= pos.highest_price * (1 - c.trailing_stop_pct / 100):
                return "stop_loss", f"移动止损{c.trailing_stop_pct:g}%"
        elif c.stop_loss_mode == "none":
            return None
        return None
