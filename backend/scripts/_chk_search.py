# -*- coding: utf-8 -*-
"""验证 list_tasks 服务端搜索生效。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

full = db.list_tasks("backtest")
hit = db.list_tasks("backtest", search="bt_f70e4260e8b2")
hit2 = db.list_tasks("backtest", search="做T层采纳")
print(f"全量 {len(full)} 条｜按ID搜 {len(hit)} 条 "
      f"{[t['task_id'] for t in hit]}｜按名搜 {len(hit2)} 条")
assert any(t["task_id"] == "bt_f70e4260e8b2" for t in hit), "ID 搜索未命中"
assert all("做T层采纳" in t["name"] for t in hit2), "名称搜索有杂质"
print("搜索验证 OK")
