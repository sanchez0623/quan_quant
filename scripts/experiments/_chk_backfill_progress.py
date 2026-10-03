# -*- coding: utf-8 -*-
"""查看最近 data_update 任务进度（临时诊断脚本）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db

rows = db.list_tasks(limit=5) if hasattr(db, "list_tasks") else None
if rows is None:
    import sqlite3
    from app import config
    con = sqlite3.connect(str(config.META_DB_PATH))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select id, name, type, status, progress, message, created_at, "
        "substr(error, 1, 300) as err from tasks where type='data_update' "
        "order by created_at desc limit 5")]
for r in rows:
    print({k: r.get(k) for k in ("name", "status", "progress",
                                 "message", "created_at", "err")})
