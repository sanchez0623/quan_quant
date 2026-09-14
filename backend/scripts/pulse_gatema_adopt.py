# -*- coding: utf-8 -*-
"""大盘闸门 MA 落库（用户拍板：采纳 MA30，MA30/MA60 都落库）。

MA30 = 采纳形态带 🏷️；MA60 = 对照形态不带标签。
防重：按任务名查 meta.db tasks 表，已存在则跳过（幂等可重跑）。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_gatema_oat import mk, _save  # noqa: E402
from app import config  # noqa: E402

JOBS = [
    ("🏷️大盘闸门采纳-MA30-全区间(分钟)", 30, False),
    ("🏷️大盘闸门采纳-MA30-OOS段(分钟)", 30, True),
    ("对照-大盘闸门MA60-全区间(分钟)", 60, False),
    ("对照-大盘闸门MA60-OOS段(分钟)", 60, True),
]


def main():
    conn = sqlite3.connect(config.META_DB_PATH)
    names = {r[0] for r in conn.execute("SELECT name FROM tasks")}
    conn.close()
    cfg0 = base_gate_on()
    for name, ma, oos in JOBS:
        if name in names:
            print(f"[跳过] {name}", flush=True)
            continue
        print(f"[落库] {name} ...", flush=True)
        _save(name, mk(cfg0, ma, oos))


if __name__ == "__main__":
    main()
