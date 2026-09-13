# -*- coding: utf-8 -*-
"""给重点有效任务打 🏷️ 标签（命名前缀），并核对落库完整性。"""
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])) if False else None
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

TAGGED = {
    "bt_f70e4260e8b2": "做T层采纳形态-asym_bias0-全区间(分钟)",
    "bt_8d53e38e564b": "做T层采纳形态-asym_bias0-OOS段(分钟)",
    "bt_8de421d00745": "P0采纳形态-轮动stale5-全区间",
    "bt_d4cc28445297": "P0采纳形态-轮动stale5-OOS段",
    "bt_c8f08b868549": "v5最终形态-阶段3寻优最优-全区间",
    "bt_8a3df255221c": "D语境-HALF_GATE(消融最佳)-全区间",
}
for tid, old in TAGGED.items():
    t = next((t for t in db.list_tasks("backtest") if t["task_id"] == tid), None)
    if t is None:
        print(f"{tid}: 未找到，跳过")
        continue
    if t["name"].startswith("🏷️"):
        print(f"{tid}: 已有标签，跳过")
        continue
    db.update_task(tid, name=f"🏷️{t['name']}")
    print(f"{tid}: '{t['name']}' -> '🏷️{t['name']}'", flush=True)
