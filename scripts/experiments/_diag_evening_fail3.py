# -*- coding: utf-8 -*-
"""诊断3：近14天盘后更新成败序列 + 异常终止任务的时间与并发关系"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3

from app import config

con = sqlite3.connect(str(config.META_DB_PATH))
con.row_factory = sqlite3.Row

print("--- 近 14 天 data_update 每日序列（按提交日聚合）---")
for r in con.execute("""
    select substr(created_at, 1, 10) as d,
           sum(case when status='success' then 1 else 0 end) as ok,
           sum(case when status='failed' then 1 else 0 end) as fail,
           sum(case when status='running' then 1 else 0 end) as run
    from tasks where type='data_update' and created_at > date('now', '-14 day')
    group by 1 order by 1"""):
    print(f"  {r['d']}: 成功{r['ok']} 失败{r['fail']} 运行中{r['run']}")

print("\n--- 各类型「任务进程异常终止」的发生时刻（看是否与 evening 并发）---")
for r in con.execute("""
    select type, created_at, finished_at, substr(error, 1, 30) as e
    from tasks where error like '%任务进程异常终止%'
    order by created_at desc limit 15"""):
    print(f"  [{r['created_at']}] {r['type']} 终止于 {r['finished_at']}  {r['e']}")

print("\n--- 异常终止任务死亡时刻 ±5 分钟内的其它 running 任务（并发证据）---")
for r in con.execute("""
    select a.type as victim, a.finished_at,
           (select group_concat(b.type || ':' || b.name, ' | ')
              from tasks b
             where b.id != a.id
               and b.created_at < a.finished_at
               and (b.finished_at > a.finished_at or b.status in ('running','pending'))
               and a.finished_at between b.created_at, datetime(b.created_at, '+90 minute')
               and b.created_at > datetime(a.finished_at, '-90 minute')
            ) as concurrent
    from tasks a
    where a.error like '%任务进程异常终止%'
    order by a.finished_at desc limit 10"""):
    print(f"  {r['victim']} 死于 {r['finished_at']} 并发: {(r['concurrent'] or '(无)')[:150]}")
