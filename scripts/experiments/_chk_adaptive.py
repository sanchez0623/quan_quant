# -*- coding: utf-8 -*-
"""读取 bt_f70e4260e8b2 基座 config 的自适应止损相关现值。"""
import json
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
row = con.execute(
    "SELECT payload FROM tasks WHERE id='bt_f70e4260e8b2'").fetchone()
cfg = json.loads(row[0])["config"]
rc = cfg["risk_config"]
for k in ("stop_loss_mode", "adaptive", "adaptive_trend_ma", "adaptive_slope_n",
          "adaptive_k_loose", "adaptive_k_tight", "adaptive_vol_n",
          "adaptive_vol_hi", "adaptive_vol_lo", "atr_trail_mult",
          "atr_trail_floor"):
    print("risk." + k, "=", rc.get(k))
