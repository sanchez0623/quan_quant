# -*- coding: utf-8 -*-
"""诊断真实业务库 data/meta.db：目标任务 vs 可见任务的登记差异。"""
import sqlite3
from pathlib import Path

dbf = Path(__file__).resolve().parents[2] / "data" / "meta.db"
con = sqlite3.connect(dbf)
try:
    cols = [c[1] for c in con.execute("PRAGMA table_info(tasks)").fetchall()]
    print("tasks cols:", cols)
    for tid in ("bt_f70e4260e8b2", "bt_8d53e38e564b", "bt_8de421d00745",
                "bt_d4cc28445297", "bt_c8f08b868549", "bt_8a3df255221c"):
        row = con.execute(
            "SELECT id, name, type, status, progress FROM tasks WHERE id=?",
            (tid,)).fetchone()
        print("  ", tid, "->", row)
finally:
    con.close()
