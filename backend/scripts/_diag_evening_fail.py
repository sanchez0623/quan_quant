# -*- coding: utf-8 -*-
"""诊断：盘后日线更新连续失败任务（09-16/09-17）完整 error 与 payload"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3

from app import config

con = sqlite3.connect(str(config.META_DB_PATH))
con.row_factory = sqlite3.Row
rows = con.execute(
    "select id, name, status, progress, message, error, created_at, finished_at, payload "
    "from tasks where type='data_update' and name like '%盘后数据更新%' "
    "order by created_at desc limit 4").fetchall()
for r in rows:
    err = r["error"] or ""
    print("=" * 70)
    print(f"[{r['created_at']}] {r['name']} status={r['status']} progress={r['progress']:.1f}")
    print(f"  message: {r['message']}")
    print(f"  finished_at: {r['finished_at']}")
    print(f"  error({len(err)} chars): {err[:500] if err else '(空)'}")
    print(f"  payload: {r['payload'][:200] if r['payload'] else '(空)'}")
