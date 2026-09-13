# -*- coding: utf-8 -*-
"""读取 bt_76889c798212 基座 config 的 pool_gate 相关现值与键位置。"""
import json
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path(__file__).resolve().parents[2] / "data" / "meta.db"))
row = con.execute(
    "SELECT payload FROM tasks WHERE id='bt_76889c798212'").fetchone()
cfg = json.loads(row[0])["config"]


def find(obj, key, path="config"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                print(f"{path}.{k} = {v}")
            find(v, key, f"{path}.{k}")


for k in ("pool_gate", "pool_gate_enter_th", "index_gate", "t_debt_max_days"):
    find(cfg, k)
print("config 顶层键：", list(cfg.keys()))
