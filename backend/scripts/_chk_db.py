# -*- coding: utf-8 -*-
"""诊断：bt_f70e4260e8b2 在哪个 db、tasks/reports 登记状态，对比可见任务。"""
import sqlite3
from pathlib import Path

for dbf in ["app.db", "app/data/meta.db"]:
    p = Path(dbf)
    if not p.exists():
        print(f"== {dbf}: 不存在")
        continue
    con = sqlite3.connect(dbf)
    try:
        tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"== {dbf}: tables={tabs}")
        if "tasks" in tabs:
            for tid in ("bt_f70e4260e8b2", "bt_8de421d00745", "bt_c8f08b868549",
                        "bt_8a3df255221c", "bt_8d53e38e564b"):
                row = con.execute(
                    "SELECT id, name, type, status, progress FROM tasks WHERE id=?",
                    (tid,)).fetchone()
                print("  ", tid, "->", row)
        if "backtest_reports" in tabs:
            cols = [c[1] for c in con.execute(
                "PRAGMA table_info(backtest_reports)").fetchall()]
            print("  backtest_reports cols:", cols)
    finally:
        con.close()
