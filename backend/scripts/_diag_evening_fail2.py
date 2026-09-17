# -*- coding: utf-8 -*-
"""诊断2：任务进程异常终止的分布（确定 SystemExit 来源范围）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3

from app import config

con = sqlite3.connect(str(config.META_DB_PATH))
print("--- 异常终止 error 分布（按类型/错误内容）---")
for r in con.execute(
        "select type, error, count(*) from tasks "
        "where error like '%任务进程异常终止%' group by type, error"):
    print(r)
print("\n--- data_update 失败原因分布（近30天）---")
for r in con.execute(
        "select substr(error, 1, 60), count(*) from tasks "
        "where type='data_update' and status='failed' and created_at > '2026-08-18' "
        "group by 1"):
    print(r)
print("\n--- data_update 成功次数（近30天）---")
for r in con.execute(
        "select status, count(*) from tasks where type='data_update' "
        "and created_at > '2026-08-18' group by status"):
    print(r)
