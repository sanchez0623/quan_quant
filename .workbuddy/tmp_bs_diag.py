# -*- coding: utf-8 -*-
"""诊断：baostock 黑名单真实状态 + 实测登录"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))

from app.data import bs_usage

info = bs_usage.tracker.last_blacklist()
print("last_blacklist 记录:", info, flush=True)
print("is_blacklisted():", bs_usage.tracker.is_blacklisted(), flush=True)
print("今日调用数:", bs_usage.tracker.daily_count(), flush=True)

from app.data.sources import BaostockSource
bs = BaostockSource()
ok = bs.health_check(timeout=15)
print("health_check(实测登录+查询):", ok, flush=True)
if ok:
    df = bs.get_daily("600000", "2026-09-01", "2026-09-07")
    print("小查询 600000:", "OK" if df is not None and df.height else "空", flush=True)
