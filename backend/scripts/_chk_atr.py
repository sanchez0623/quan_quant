# -*- coding: utf-8 -*-
"""读取 bt_76889c798212 基座的 ATR 止损参数现值。"""
import json
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
row = con.execute(
    "SELECT payload FROM tasks WHERE id='bt_76889c798212'").fetchone()
rc = json.loads(row[0])["config"]["risk_config"]
for k in ("stop_loss_mode", "atr_multiplier", "atr_trail_mult",
          "atr_cost_base", "atr_trail_floor", "adaptive"):
    print("risk." + k, "=", rc.get(k))
