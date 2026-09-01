# -*- coding: utf-8 -*-
"""P0-1b 守卫：引擎层对 bar 的所有字段读取必须被物化白名单覆盖。

静态扫描 broker/risk/runner 源码中的字面量访问（bar.get("k") / bar["k"]），
逐一断言 ∈ BAR_KEEP_COLS 或命中动态规则（atr{N} 数字列 / adaptive_ 前缀）。
引擎新增 bar 读取而未登记白名单时，本测试显式失败——防止 _simulate 物化
丢列后 .get 静默返回 None 的隐性错误。

动态 f-string 访问（如 f"atr{risk_cfg.atr_period}"）无法静态捕获，
必须命中已登记的动态规则；新增动态形态时同步扩充 runner._BAR_KEEP_DYNAMIC。
"""
import re
from pathlib import Path

from app.engine import broker, risk, runner

# bar 字面量访问的两种形态（\b 防止 bars[code] / xxx_bar[ 误匹配）
_ACCESS_PATTERNS = (
    re.compile(r'\bbar\.get\(\s*["\']([^"\']+)["\']'),
    re.compile(r'\bbar\[\s*["\']([^"\']+)["\']\s*\]'),
)


def _literal_bar_keys(module) -> dict:
    """从模块源码提取 bar 字面量读取键 -> {键: 首次出现的模块名}"""
    src = Path(module.__file__).read_text(encoding="utf-8")
    found: dict = {}
    for pat in _ACCESS_PATTERNS:
        for k in pat.findall(src):
            found.setdefault(k, module.__name__)
    return found


def test_bar_whitelist_covers_engine_reads():
    found: dict = {}
    for mod in (broker, risk, runner):
        found.update(_literal_bar_keys(mod))
    missing = {k: m for k, m in found.items()
               if not runner._bar_col_allowed(k)}
    assert not missing, (
        f"引擎新增了 bar 字段读取但未登记物化白名单: {missing}；"
        f"请同步 runner.BAR_KEEP_COLS（或扩充 _BAR_KEEP_DYNAMIC 动态规则），"
        f"否则 _simulate 白名单物化会静默丢列（.get 得 None）")


def test_bar_whitelist_no_dead_entries():
    """反向守卫：白名单不应有从未被引擎读取的死键（防长期腐化）。
    动态规则覆盖的键（atr{N}/adaptive_*）不在静态扫描范围，不在此列。"""
    found: dict = {}
    for mod in (broker, risk, runner):
        found.update(_literal_bar_keys(mod))
    dead = sorted(k for k in runner.BAR_KEEP_COLS if k not in found)
    assert not dead, f"白名单存在从未被引擎读取的死键: {dead}（确认后删除）"
