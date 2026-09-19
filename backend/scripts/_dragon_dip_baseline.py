# -*- coding: utf-8 -*-
"""龙头低吸（dragon_dip）基线回测落库。

- 全市场在市静态池（情绪指标与连板排名需要全市场口径）
- 默认参数 + 固定止损 5%（框架：次日低开低走破 5% 止损）
- max_holdings=2 与 top_n=2 一致（两处一致规则）
- 落库三件套：create_task(tag=重点) + save_report + update_task(success)
"""
import json
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app import db
from app.data import store
from app.engine import runner

REPORTS = Path(BACKEND).parents[1] / "data" / "reports"


def build_cfg() -> dict:
    basic = store.read_stock_basic()
    universe = (basic.filter(~pl.col("delisted"))["code"].to_list()
                if basic is not None and "delisted" in basic.columns
                else basic["code"].to_list())
    return {
        "name": "龙头低吸基线-默认参数-全区间",
        "strategy_id": "dragon_dip",
        "period": "daily",
        "universe": universe,
        "start_date": "2024-01-02",
        "end_date": "2026-09-18",
        "initial_capital": 1_000_000.0,
        "params": {},
        "risk_config": {
            "stop_loss_mode": "fixed",
            "stop_loss_pct": 5.0,
            "max_holdings": 2,
            "max_position_pct_per_stock": 60,
            "max_total_position_pct": 100,
            "max_drawdown_breaker": 30,
        },
    }


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    cfg = build_cfg()
    print(f"universe {len(cfg['universe'])} 只，开始回测…", flush=True)
    rep = runner.run_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str),
                    encoding="utf-8")
    payload = {"strategy_id": cfg["strategy_id"], "period": cfg["period"],
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, cfg["name"], "backtest", payload, tag="重点")
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    trades = len(rep.get("trade_log") or [])
    print(f"{tid}  收益 {m.get('total_return', 0):+.2%}  "
          f"最大回撤 {m.get('max_drawdown', 0):+.2%}  交易 {trades} 笔",
          flush=True)


if __name__ == "__main__":
    main()
