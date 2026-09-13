# -*- coding: utf-8 -*-
"""读取 bt_f76c49b84124 基座 config 的仓位相关现值。"""
import json
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
row = con.execute(
    "SELECT payload FROM tasks WHERE id='bt_f76c49b84124'").fetchone()
cfg = json.loads(row[0])["config"]
rc, p = cfg["risk_config"], cfg["params"]
for k in ("max_holdings", "max_total_position_pct", "cash_reserve_pct",
          "max_position_pct_per_stock"):
    print("risk." + k, "=", rc.get(k))
for k in ("max_holdings", "base_pct_min", "base_pct_max", "pool_n"):
    print("params." + k, "=", p.get(k))
