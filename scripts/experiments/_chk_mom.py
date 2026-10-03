# -*- coding: utf-8 -*-
"""读取 bt_76889c798212 基座的动量窗口参数现值。"""
import json
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
row = con.execute(
    "SELECT payload FROM tasks WHERE id='bt_76889c798212'").fetchone()
p = json.loads(row[0])["config"]["params"]
for k in ("mom_short", "mom_mid", "mom_long", "w_short", "w_mid", "w_accel"):
    print("params." + k, "=", p.get(k))
