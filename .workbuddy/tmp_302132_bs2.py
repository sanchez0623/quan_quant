# -*- coding: utf-8 -*-
"""用登录封装测 baostock 对 302132 的日线/5分钟线"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data.sources import BaostockSource

bs = BaostockSource()
d = bs.get_daily("302132", "2023-04-03", "2023-04-07")
print("日线 2023-04:", None if d is None else (d.height, d['date'].min(), d['date'].max()), flush=True)
m = bs.get_minute5("302132", "2023-04-03", "2023-04-07")
print("5分钟 2023-04:", None if m is None else (m.height, m['date'].min(), m['date'].max()), flush=True)
