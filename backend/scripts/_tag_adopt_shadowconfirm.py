# -*- coding: utf-8 -*-
"""影线承接升级打 🏷️（AB 复验位级一致，用户拍板：两档都打标，实盘跑 z1.0）。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db  # noqa: E402

PAIRS = [
    ("因子扩展2-影线承接升级-z1.0-全区间(分钟)",
     "🏷️影线承接升级采纳z1.0-全区间(分钟)"),
    ("因子扩展2-影线承接升级-z1.0-OOS段(分钟)",
     "🏷️影线承接升级采纳z1.0-OOS段(分钟)"),
    ("因子扩展2-影线承接升级-z0.75-全区间(分钟)",
     "🏷️影线承接对照z0.75-全区间(分钟)"),
    ("因子扩展2-影线承接升级-z0.75-OOS段(分钟)",
     "🏷️影线承接对照z0.75-OOS段(分钟)"),
]

conn = sqlite3.connect(config.META_DB_PATH)
for old, new in PAIRS:
    row = conn.execute("SELECT id FROM tasks WHERE name=?", (old,)).fetchone()
    if not row:
        print(f"[MISSING] {old}")
        continue
    db.update_task(row[0], name=new)
    print(f"[🏷️] {row[0]}  {old} -> {new}")
conn.close()
