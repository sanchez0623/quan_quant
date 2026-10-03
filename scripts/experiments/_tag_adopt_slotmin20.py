# -*- coding: utf-8 -*-
"""试仓占比 20 采纳打 🏷️（AB 复验过线，改名 OAT 载体任务）。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db  # noqa: E402

PAIRS = [
    ("试仓占比20-全区间(分钟)", "🏷️试仓占比采纳20-全区间(分钟)"),
    ("试仓占比20-OOS段(分钟)", "🏷️试仓占比采纳20-OOS段(分钟)"),
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
