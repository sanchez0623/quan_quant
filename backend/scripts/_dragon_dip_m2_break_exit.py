# -*- coding: utf-8 -*-
"""打板退出纪律对照 + yin+dip 新默认组合回测（用户指令 2026-09-28）。

- E1 默认组合（yin+dip）：多选新默认的首次基线
- E2 纯打板 + 破板清仓：dban_exit=break（收盘未封涨停 -> 次根清仓）
- E3 纯打板 + 固定止损（对照）：dban_exit=stop
全部落库三件套（tag=重点：打板纪律结论载体）。
"""
import json
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import db
from app.engine.dragon_dip_minute import run_minute_backtest

REPORTS = BACKEND.parent / "data" / "reports"

EXPS = [
    ("E1 默认组合yin+dip", {"entry_type": ["yin", "dip"]}, {}),
    ("E2 纯打板+破板清仓", {"entry_type": ["dban"], "dban_exit": "break"}, {}),
    ("E3 纯打板+固定止损", {"entry_type": ["dban"], "dban_exit": "stop"}, {}),
]

for name, p, r_over in EXPS:
    cfg = {"name": f"龙头低吸二期 {name}-2024全区间",
           "start_date": "2024-01-02", "end_date": "2026-09-24",
           "initial_capital": 1_000_000.0, "params": p,
           "risk_config": {"stop_loss_pct": 5.0, "max_holdings": 5,
                           "max_position_pct_per_stock": 60,
                           "max_drawdown_breaker": 30, **r_over}}
    print(f">>> {name} …", flush=True)
    rep = run_minute_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str),
                    encoding="utf-8")
    db.create_task(tid, cfg["name"], "backtest",
                   {"strategy_id": "dragon_dip_minute", "period": "minute5",
                    "config": cfg, "report_path": str(path)}, tag="重点")
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    wd = (rep.get("withdrawal") or {})
    print(f"    {tid} 收益 {m.get('total_return', 0):+.2%} "
          f"回撤 {m.get('max_drawdown', 0):+.2%} 夏普 {m.get('sharpe', 0):.2f} "
          f"交易 {m.get('total_trades', 0)} 提取{wd.get('nav_times', 0)}次"
          f"/{wd.get('total', 0):,.0f}元", flush=True)
