# -*- coding: utf-8 -*-
"""一次性迁移：🏷️ 名称前缀 → tasks.tag 字段（'重点'）+ 名称去前缀。

db.init_db() 已保证 tag 列存在（_migrate 幂等补列）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db  # noqa: E402

db.init_db()  # 确保 tag 列已补

conn = sqlite3_conn = None
import sqlite3  # noqa: E402
conn = sqlite3.connect(config.META_DB_PATH)
rows = conn.execute("SELECT id, name FROM tasks WHERE name LIKE '🏷️%'").fetchall()
print(f"发现 {len(rows)} 个 🏷️ 前缀任务")
for tid, name in rows:
    new_name = name.replace("🏷️", "").lstrip()
    db.update_task(tid, name=new_name, tag="重点")
    print(f"  {tid}  '{name}' -> name='{new_name}' tag='重点'")
conn.close()
print("迁移完成")
