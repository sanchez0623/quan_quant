# -*- coding: utf-8 -*-
"""金字塔加仓 P1 防同价 + P2 最小有效量测试。

背景（600339 案例 bt_309f9613c9b5）：开仓 5 分钟后即"突破20日新高"同价加仓
74,600 股（存量新高 + 冷却期初始 -1e9 恒满足），第 2 次加仓在现金耗尽后
缩量到 100 股。修复：
- P1：冷却期自开仓日起算（last_add_idx=开仓日）+ 新高须发生在建仓之后
  （close > 开仓以来最高收盘）——同价/未创新高一律不加仓
- P2：加仓预算 < ADD_MIN_BUDGET_PCT%（占总资产）时信号层跳过且不消耗
  次数/冷却；执行层同口径守卫拒绝缩量垃圾单

测试用 _walk 直调手造日线特征列（atr_pct=None 关闭做T分支，日线无时间戳
天然跳过时点T），聚焦状态机本身。
"""
import polars as pl

from app.engine import momentum_core as mc
from app.engine.strategies.momentum_t import MomentumTStrategy


def _walk_rows(closes, p):
    """手造 momentum_t 日线特征列并直调 _walk，返回 [(date, tag, reason, budget_pct)]"""
    strat = MomentumTStrategy()
    days = [f"2025-01-{d:02d}" for d in range(1, len(closes) + 1)]
    df = pl.DataFrame({
        "date": days,
        "close": closes,
        "atr_pct": [None] * len(closes),          # 关闭做T分支
        "bias": [1.0] * len(closes),              # 远离止损/过热区间
        "vol_pos": [None] * len(closes),
        "breakout": [True] * len(closes),         # 存量 20 日新高恒成立（入选特征）
        "dif": [0.1] * len(closes), "dea": [0.05] * len(closes),   # 金叉恒成立
        "ma_slow": [c * 0.9 for c in closes],     # 收盘恒站上慢线
        "slope": [0.1] * len(closes),             # 斜率向上恒成立（满配开仓）
        "day_idx": list(range(len(closes))),
        "pool_gate": [False] * len(closes),
    })
    cols = strat._walk(df, p, set(days), None)
    out = df.with_columns(cols).to_dicts()
    return [(r["date"], r["tag"], r["reason"], r["budget_pct"]) for r in out]


def _params(**over):
    p = {k["key"]: k["default"] for k in MomentumTStrategy().param_schema}
    p.update(over)
    return p


# ---------------- P1：防同价 / 新高须发生在建仓之后 ----------------

def test_p1_no_add_on_flat_after_open():
    """开仓后横盘（同价/存量新高）：即便 breakout 恒成立、冷却期满，也不加仓"""
    rows = _walk_rows([10.0] * 8, _params())
    opens = [r for r in rows if r[1] == "开仓"]
    assert len(opens) == 1 and opens[0][0] == "2025-01-01"
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert not adds, f"横盘期不应有任何金字塔加仓，实际: {adds}"


def test_p1_no_add_on_recover_to_open_price():
    """开仓后回落再回到开仓价：未创开仓以来新高（严格大于），不加仓"""
    closes = [10.0, 9.8, 9.9, 9.95, 9.99, 10.0, 10.0, 10.0]
    rows = _walk_rows(closes, _params())
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert not adds, f"仅回到开仓价未创新高不应加仓，实际: {adds}"


def test_p1_add_after_new_high_and_cooldown():
    """开仓后持续创新高 + 冷却期满（>=add_cooldown 交易日）：正常金字塔加仓"""
    closes = [10.0, 10.05, 10.10, 10.15, 10.20, 10.25, 10.30, 10.35]
    p = _params(add_cooldown=5, max_adds=3, add_scale=0.5)
    rows = _walk_rows(closes, p)
    opens = [r for r in rows if r[1] == "开仓"]
    assert len(opens) == 1 and opens[0][0] == "2025-01-01"
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert len(adds) == 1, f"第6日应恰好触发1次加仓，实际: {adds}"
    assert adds[0][0] == "2025-01-06", f"首加应在冷却期满日(day6)，实际: {adds[0]}"
    assert adds[0][3] == p["base_pct_max"] * 0.5, "首加预算应为 base_max×add_scale"


def test_p1_second_add_requires_new_high_again():
    """第2次加仓须再创新高 + 再次冷却：高位横盘不连续触发"""
    # 冷却2：day3 首加(10.10>10.05)，day5 第2次(10.20>10.15)，
    # day6 起横盘 10.25 未创新高 -> 不再加仓
    closes = [10.0, 10.05, 10.10, 10.15, 10.20, 10.25,
              10.25, 10.25, 10.25, 10.25, 10.25, 10.25]
    rows = _walk_rows(closes, _params(add_cooldown=2, max_adds=3))
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert [(r[0], r[2]) for r in adds] == [
        ("2025-01-03", "突破20日新高，第1次金字塔加仓"),
        ("2025-01-05", "突破20日新高，第2次金字塔加仓"),
    ], f"横盘期不应连续加仓，实际: {adds}"


# ---------------- P2：加仓最小有效量 ----------------

def test_p2_skip_when_budget_below_threshold():
    """预算低于 ADD_MIN_BUDGET_PCT%：跳过且不产生加仓信号"""
    # base_pct_max=0.8 -> 首加预算 0.4% < 0.5% -> 全程无金字塔加仓
    closes = [10.0, 10.05, 10.10, 10.15, 10.20, 10.25, 10.30, 10.35, 10.40]
    rows = _walk_rows(closes, _params(base_pct_max=0.8, add_cooldown=2))
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert not adds, f"预算不足最小有效量不应加仓，实际: {adds}"


def test_p2_pass_when_budget_above_threshold():
    """对照：base_pct_max=1.2 -> 首加预算 0.6% >= 0.5% -> 正常触发"""
    closes = [10.0, 10.05, 10.10, 10.15, 10.20, 10.25, 10.30]
    rows = _walk_rows(closes, _params(base_pct_max=1.2, add_cooldown=2))
    adds = [r for r in rows if "金字塔加仓" in (r[2] or "")]
    assert len(adds) == 1 and adds[0][0] == "2025-01-03", \
        f"预算达标应正常加仓，实际: {adds}"


def test_p2_threshold_constant():
    """共享常量存在且语义为占总资产百分比"""
    assert mc.ADD_MIN_BUDGET_PCT == 0.5
