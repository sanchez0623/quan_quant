# -*- coding: utf-8 -*-
"""风控模块（与策略参数分离）：仓位上限、回撤熔断、日内交易次数、止损/止盈/移动止损"""


class RiskConfig:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.max_position_pct_per_stock = float(cfg.get("max_position_pct_per_stock", 40))
        self.max_total_position_pct = float(cfg.get("max_total_position_pct", 100))
        # fixed | atr | trailing | atr_trailing
        self.stop_loss_mode = cfg.get("stop_loss_mode", "atr_trailing")
        self.stop_loss_pct = float(cfg.get("stop_loss_pct", 12.0))
        self.atr_period = int(cfg.get("atr_period", 14))
        # 默认组面向"做T+动量"风格：ATR 2.0 在高波动票上反复扫损，放宽至 2.5（建议 2.5~3）
        self.atr_multiplier = float(cfg.get("atr_multiplier", 2.5))
        self.take_profit_pct = float(cfg.get("take_profit_pct", 40) or 0)
        self.trailing_stop_pct = float(cfg.get("trailing_stop_pct", 5.0) or 0)
        self.max_drawdown_breaker = float(cfg.get("max_drawdown_breaker", 30))
        # max_intraday_trades 语义：单只股票每日最大交易次数（None/缺失 -> 默认 4）
        self.max_intraday_trades = int(cfg.get("max_intraday_trades") or 4)
        self.max_holdings = int(cfg.get("max_holdings", 0) or 0)  # 最大持仓只数，0=不限
        self.cash_reserve_pct = float(cfg.get("cash_reserve_pct", 1.5) or 0)  # 现金缓冲比例
        # ---- 组合层：板块集中度上限 ----
        # 单板块持仓市值（核心仓+做T仓合计）≤ 净值 × max_sector_pct/100。
        # 0=不启用。仅拦截超限板块的【开仓/加仓】（缩量或拒单），不主动卖出、
        # 不限制做T还债/纯正向T，避免破坏做T收益结构。板块口径见 sources.derive_board
        # （main主板/chinext创业板/star科创板/bse北交所）。
        self.max_sector_pct = float(cfg.get("max_sector_pct", 0) or 0)
        # ---- ATR_TRAILING：止损线 = max(成本项 − k1×ATR, 最高价 − k2×ATR)，只上不下 ----
        # k1：硬止损兜底倍数（相对成本）。k2：移动锁盈倍数（相对持仓期最高价）。
        # k2 < k1 时，价格上涨后移动项会超过成本项并接管，实现「随最高价上移锁盈」。
        # 时序样本外验证（2024选参/2025测试）表明：k2 的「最优值」不可预测
        # （训练集排名与测试集排名不相关），但 5~12 区间整体稳健，<=3 明显偏紧。
        self.atr_trail_mult = float(cfg.get("atr_trail_mult", 6.0) or 0)
        # 成本基准：first=首笔开仓价（不受加仓抬高，推荐）｜wavg=加权平均成本（同旧 ATR 口径）
        self.atr_cost_base = str(cfg.get("atr_cost_base", "first") or "first").lower()
        # 止损线棘轮：True=只上不下（推荐）；False=允许随最高价回落而下移
        self.atr_trail_floor = bool(cfg.get("atr_trail_floor", True))
        # ---- 自适应止损：按市场状态缩放 k1/k2 ----
        # off=关闭｜trend=个股趋势状态（收盘价 vs 均线 + 均线斜率）｜vol=波动率分位
        self.adaptive = str(cfg.get("adaptive", "trend") or "trend").lower()
        self.adaptive_trend_ma = int(cfg.get("adaptive_trend_ma", 60) or 60)
        self.adaptive_slope_n = int(cfg.get("adaptive_slope_n", 5) or 5)
        # 趋势确立（价在均线上且均线走平/向上）-> 放宽止损，让利润奔跑
        self.adaptive_k_loose = float(cfg.get("adaptive_k_loose", 1.5) or 1.0)
        # 趋势破坏（价跌破均线）-> 收紧止损，快速离场
        self.adaptive_k_tight = float(cfg.get("adaptive_k_tight", 0.7) or 1.0)
        # vol 模式：ATR% 滚动分位阈值
        self.adaptive_vol_n = int(cfg.get("adaptive_vol_n", 120) or 120)
        self.adaptive_vol_hi = float(cfg.get("adaptive_vol_hi", 0.7) or 0.7)
        self.adaptive_vol_lo = float(cfg.get("adaptive_vol_lo", 0.3) or 0.3)
        # ---- 双层止损（方案B）：交易仓(做T)用独立档，核心仓(开仓/加仓)沿用默认档 ----
        # trade_tier_on=off 时所有 group 走默认档，行为与现状一致。
        # 默认档参数经敏感度实测（bt_84f3d9c10301 数据集）取最优：sp=10 成本底线放
        # 宽到正常波动不扫损（做T需要"等反弹"呼吸空间），tm=5 保留极端下跌保护。
        self.trade_tier_on = bool(cfg.get("trade_tier_on", False))
        self.trade_atr_mult = float(cfg.get("trade_atr_mult", 3.0) or 0)      # 交易仓硬止损倍数 k1
        self.trade_trail_mult = float(cfg.get("trade_trail_mult", 5.0) or 0)  # 交易仓移动锁盈倍数 k2
        self.trade_stop_pct = float(cfg.get("trade_stop_pct", 10.0) or 0)     # 交易仓成本底线(%)
        # ---- 方案E：市况条件化保护 ----
        # regime_b_on=on 时，双层止损(做T仓独立档)只在「趋势市」启用；
        # 震荡/下跌市做T仓退回默认档（BASE 行为），规避低波动市 B 档的拖累。
        # 市况由 runner 逐日按指数判定写入 RiskManager.current_regime（T-1 对齐）。
        self.regime_b_on = bool(cfg.get("regime_b_on", False))


class RiskManager:
    """runner 在每bar撮合前调用检查"""

    def __init__(self, config: RiskConfig):
        self.cfg = config
        self.peak_equity: float = 0.0
        self.broken: bool = False  # 回撤熔断：停止开新仓
        self.trough_equity: float | None = None  # 熔断后的最低净值（用于企稳判定）
        self.stable: bool = False  # 净值企稳：回撤不再扩大（等待策略开仓信号解除熔断）
        self.current_regime: str = "range"  # 方案E：当前市场状态（trend/range/crash，runner 逐日写入）

    # ---------------- 买入前检查 ----------------

    def update_equity(self, equity: float) -> None:
        """每bar更新净值，检查回撤熔断与企稳。

        熔断规则：
        - 首次回撤跌破阈值 → 触发熔断（禁止开新仓）；
        - 熔断中净值继续创新低 → 重置企稳状态（仍不稳）；
        - 熔断中净值不再创新低（回撤不再扩大，空仓横盘亦算企稳）→ 待策略开仓信号解除；
        - 回撤修复到阈值以内 → 直接解除熔断。
        避免"熔断后空仓横盘、回撤永不修复"导致的永久停摆。"""
        self.peak_equity = max(self.peak_equity, equity)
        if self.cfg.max_drawdown_breaker <= 0 or self.peak_equity <= 0:
            return
        dd = 1 - equity / self.peak_equity
        if dd * 100 >= self.cfg.max_drawdown_breaker:
            if not self.broken:
                self.broken = True
                self.trough_equity = equity
                self.stable = False
            elif equity < (self.trough_equity or equity):
                self.trough_equity = equity  # 创新低：净值仍在恶化，重置企稳
                self.stable = False
            else:
                self.stable = True
        else:
            # 回撤修复到阈值以内：净值回到安全区，直接解除熔断
            self.broken = False
            self.stable = False
            self.trough_equity = None

    def try_resume(self, buy_signal: bool) -> bool:
        """尝试解除熔断：净值已企稳（回撤不再扩大）且当前是策略主动建仓信号（开仓/加仓）。
        返回是否已解除（解除后本次信号可继续执行）。"""
        if self.broken and self.stable and buy_signal:
            self.broken = False
            self.stable = False
            self.trough_equity = None
            return True
        return False

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
        """计算允许的买入金额上限（含现金缓冲约束：部分资金永不进场）"""
        cap_stock = equity * self.cfg.max_position_pct_per_stock / 100
        by_stock = max(0.0, cap_stock - code_market_value)
        # 总仓位上限与现金缓冲取更紧者：可投资金 = equity × (1 - cash_reserve_pct)
        cap_total = min(equity * self.cfg.max_total_position_pct / 100,
                        equity * (1 - self.cfg.cash_reserve_pct / 100))
        by_total = max(0.0, cap_total - total_market_value)
        return max(0.0, min(by_stock, by_total, cash))

    def sector_budget(self, equity: float, sector_mv: dict[str, float],
                      sector: str | None) -> float:
        """组合层：板块集中度剩余额度（该板块还可买多少）。

        max_sector_pct>0 且 code 能识别板块时生效：返回 板块上限 − 该板块已持仓市值；
        未启用/无法识别板块返回 +inf（不限制）。由 execute_buy 对开仓/加仓预算取 min。
        做T还债/纯正向T不调用本方法，保证做T收益结构不受组合层干扰。"""
        c = self.cfg
        if c.max_sector_pct <= 0 or not sector:
            return float("inf")
        cap = equity * c.max_sector_pct / 100
        return max(0.0, cap - sector_mv.get(sector, 0.0))

    # ---------------- 持仓中检查（返回 (action, reason)） ----------------

    def check_stop(self, pos, price: float, atr: float | None,
                   bar: dict | None = None) -> tuple[str, str] | None:
        """返回 (动作: stop_loss|take_profit, reason) 或 None。

        bar 为当前行情字典，供自适应止损读取趋势/波动列；不传则自适应退化为中性倍数 1.0。
        方案B（双层止损）：交易仓(tag=做T)用独立档，核心仓沿用默认档。
        方案E（市况条件化）：regime_b_on 开启时，B 档只在 trend 市**激活**（
        粘滞：trend 出现一次即锁定到平仓，只在非趋势持仓期退回默认档），
        避免日级市况切换导致做T仓止损档位中途跳变（宽→紧扫损）。"""
        c = self.cfg
        is_trade = c.trade_tier_on and pos.tag == "做T"
        if c.regime_b_on:
            if is_trade and self.current_regime == "trend":
                pos.b_tier = True  # 粘滞激活：trend 出现一次即锁定 B 档
            is_trade = is_trade and pos.b_tier
        if not is_trade and c.take_profit_pct > 0 and price >= pos.cost_price * (1 + c.take_profit_pct / 100):
            return "take_profit", f"止盈{c.take_profit_pct:g}%"
        if is_trade:
            # 交易仓(做T)：成本底线 + 独立 ATR 移动线（做T仓不参与固定止盈，靠网格高抛/止损离场）
            if c.trade_stop_pct > 0 and price <= pos.cost_price * (1 - c.trade_stop_pct / 100):
                return "stop_loss", f"T仓成本止损{c.trade_stop_pct:g}%"
            if atr is None or atr <= 0:
                return None
            k1 = c.trade_atr_mult or c.atr_multiplier
            k2 = c.trade_trail_mult or c.atr_trail_mult
            cost_base = pos.first_price if c.atr_cost_base == "first" else pos.cost_price
            cost_line = cost_base - atr * k1
            trail_line = pos.highest_price - atr * k2
            stop = max(cost_line, trail_line)
            if c.atr_trail_floor:
                pos.trail_stop = max(pos.trail_stop, stop)
                stop = pos.trail_stop
            else:
                pos.trail_stop = stop
            if price <= stop:
                return "stop_loss", f"T仓ATR止损(k1={k1:.2f},k2={k2:.2f})"
            return None
        if c.stop_loss_mode == "fixed":
            if price <= pos.cost_price * (1 - c.stop_loss_pct / 100):
                return "stop_loss", f"固定止损{c.stop_loss_pct:g}%"
        elif c.stop_loss_mode == "atr":
            if atr is not None and price <= pos.cost_price - atr * c.atr_multiplier:
                return "stop_loss", f"ATR止损x{c.atr_multiplier:g}"
        elif c.stop_loss_mode == "trailing":
            if c.trailing_stop_pct > 0 and price <= pos.highest_price * (1 - c.trailing_stop_pct / 100):
                return "stop_loss", f"移动止损{c.trailing_stop_pct:g}%"
        elif c.stop_loss_mode == "atr_trailing":
            hit = self._check_atr_trailing(pos, price, atr, bar)
            if hit:
                return hit
        elif c.stop_loss_mode == "none":
            return None
        return None

    # ---------------- ATR 移动止损 ----------------

    def _adaptive_mult(self, pos, bar: dict | None) -> float:
        """按市场状态返回止损 ATR 倍数的缩放系数。

        trend：收盘价在均线上方且均线走平/向上 -> 趋势市，放宽止损（让利润奔跑）；
               跌破均线 -> 趋势破坏，收紧止损（快速离场）；其余中性。
        vol  ：ATR% 处于自身历史高分位 -> 高波动，放宽（防噪音扫损）；
               低分位 -> 收紧（让止盈更敏感）。
        无法判定时一律返回 1.0，保证行为可退化。"""
        c = self.cfg
        if c.adaptive == "off" or not bar:
            return 1.0
        if c.adaptive == "trend":
            ma = bar.get("adaptive_ma")
            close = bar.get("close")
            if ma is None or close is None:
                return 1.0
            if close <= ma:
                return c.adaptive_k_tight
            # 价在均线上方：均线走平或向上才算趋势确立
            slope = bar.get("adaptive_slope")
            if slope is None:
                return 1.0
            return c.adaptive_k_loose if slope >= 0 else 1.0
        if c.adaptive == "vol":
            q = bar.get("adaptive_vol_q")
            if q is None:
                return 1.0
            if q >= c.adaptive_vol_hi:
                return c.adaptive_k_loose
            if q <= c.adaptive_vol_lo:
                return c.adaptive_k_tight
            return 1.0
        return 1.0

    def _check_atr_trailing(self, pos, price: float, atr: float | None,
                            bar: dict | None) -> tuple[str, str] | None:
        """止损线 = max(成本项 − k1×ATR, 最高价 − k2×ATR)，棘轮只上不下。

        成本项用首笔开仓价而非加权成本，切断「金字塔加仓抬高成本 -> 止损线抬高
        -> 小幅回调即假止损」的死循环；移动项随持仓期最高价上移，实现锁盈。"""
        if atr is None or atr <= 0:
            return None
        c = self.cfg
        m = self._adaptive_mult(pos, bar)
        k1, k2 = c.atr_multiplier * m, c.atr_trail_mult * m
        if k2 <= 0:      # 未配置移动倍数时退化为纯 ATR 止损（成本项仍用所选基准）
            k2 = k1
        cost_base = pos.first_price if c.atr_cost_base == "first" else pos.cost_price
        cost_line = cost_base - atr * k1          # 硬止损兜底
        trail_line = pos.highest_price - atr * k2  # 随最高价上移
        stop = max(cost_line, trail_line)
        if c.atr_trail_floor:
            # 棘轮：止损线只上不下，避免最高价回落后止损线跟着下移导致利润敞口扩大
            pos.trail_stop = max(pos.trail_stop, stop)
            stop = pos.trail_stop
        else:
            pos.trail_stop = stop
        if price <= stop:
            tag = "ATR移动止损"
            if m != 1.0:
                tag += f"[自适×{m:g}]"
            return "stop_loss", f"{tag}(k1={k1:.2f},k2={k2:.2f})"
        return None
