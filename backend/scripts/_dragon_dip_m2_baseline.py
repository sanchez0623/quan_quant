# -*- coding: utf-8 -*-
"""龙头低吸二期（分钟级）基线回测落库。

- 全市场在市非 ST 静态口径（Stage1 上下文）+ 分钟级事件模拟（Stage2）
- 默认参数 + 固定止损 5%；max_holdings=2 与 top_n=2 一致
- dban_fill 两种语义各跑一版：break=保守（仅炸板成交）/ touch=乐观（触板即成交）
- 落库三件套：create_task(tag=重点) + save_report + update_task(success)
用法：python scripts/_dragon_dip_m2_baseline.py [break|touch]
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


def main() -> None:
    fill = sys.argv[1] if len(sys.argv) > 1 else "break"
    REPORTS.mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": f"龙头低吸二期分钟级基线-{fill}-2024全区间",
        "start_date": "2024-01-02",
        "end_date": "2026-09-24",
        "initial_capital": 1_000_000.0,
        "params": {"dban_fill": fill},
        "risk_config": {"stop_loss_pct": 5.0, "max_holdings": 5,
                        "max_position_pct_per_stock": 60,
                        "max_total_position_pct": 100,
                        "max_drawdown_breaker": 30},
    }
    print(f"开始二期分钟级基线（dban_fill={fill}）…", flush=True)
    rep = run_minute_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str),
                    encoding="utf-8")
    payload = {"strategy_id": "dragon_dip_minute", "period": "minute5",
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, cfg["name"], "backtest", payload, tag="重点")
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    print(f"{tid}  收益 {m.get('total_return', 0):+.2%}  "
          f"最大回撤 {m.get('max_drawdown', 0):+.2%}  "
          f"夏普 {m.get('sharpe', 0):.2f}  交易 {m.get('total_trades', 0)} 笔",
          flush=True)


if __name__ == "__main__":
    main()
