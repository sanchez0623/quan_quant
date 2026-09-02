# -*- coding: utf-8 -*-
"""盘后流程（LIVE_SIGNAL_SYSTEM §5 盘后，15:30）：当日分钟线落库 + 对账卡推送。

- 分钟线落库：池子 ∪ 持仓 ∪ 状态机跟踪的票，mootdx/新浪拉当日完成 bar，
  与库内已有数据合并去重后原子写回（write_minute5 为整文件覆盖，必须先并后写）。
- 对账卡：虚拟持仓 vs 现价快照 + 虚拟权益，推送提醒用户与券商持仓核对
  （差异由用户在界面「以券商为准校准」，系统不自动改账）。
- 状态机快照/做T债务到期：t_mode=off 起步无债务；状态机已在盘中实时落库。
"""
import json
from datetime import datetime

import polars as pl

from .. import db
from ..data import store
from . import feishu, intraday, quotes


def run_postclose(data_dir=None, push: bool = True,
                  now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    cfg = intraday._live_cfg()
    pool_state = db.get_live_pool()
    positions = db.list_live_positions()
    states = db.get_strategy_states()
    pool_codes = [x["code"] for x in (pool_state.get("pool") or [])]
    codes = sorted(set(pool_codes) | {p["code"] for p in positions}
                   | set(states))

    saved: list[str] = []
    skipped: list[str] = []
    for code in codes:
        try:
            old_full = store.read_minute5(code, data_dir=data_dir)  # 全量历史
            has_today = (old_full is not None and old_full.height
                         and old_full.filter(
                             pl.col("date").str.slice(0, 10) == today).height > 0)
            if has_today:
                continue  # 已有当日数据（重复执行幂等）
            df = quotes.fetch_minute5(code, today)
            if df is None or not df.height:
                skipped.append(code)
                continue
            done = quotes.completed_bars(df, now)
            if not done.height:
                skipped.append(code)
                continue
            new = done.select(["code", "date", "open", "high", "low",
                               "close", "volume", "amount"])
            merged = (pl.concat([old_full, new], how="diagonal")
                      if old_full is not None and old_full.height else new)
            merged = (merged.unique(subset=["date"], keep="last")
                      .sort("date"))
            store.write_minute5(code, merged, data_dir)
            saved.append(code)
        except Exception:
            skipped.append(code)

    qt_map = quotes.realtime_quotes(codes)
    prices = {c: q["price"] for c, q in qt_map.items() if q.get("price")}
    equity, cash = intraday._virtual_equity(cfg, positions, prices)

    lines = [f"【盘后对账 {today}】",
             f"分钟线落库 {len(saved)} 只" +
             (f"，失败 {len(skipped)} 只" if skipped else ""),
             f"虚拟权益 {equity:,.0f}｜可用现金 {cash:,.0f}",
             "虚拟持仓（请与券商 App 核对，差异在界面校准）："]
    for p in positions:
        px = prices.get(p["code"], p["cost_price"])
        pnl = (px - p["cost_price"]) * p["volume"]
        lines.append(f"  {p['code']} {p['name']} {p['volume']}股 "
                     f"成本{p['cost_price']} 现价{px} "
                     f"浮盈{pnl:+,.0f} 开仓{p.get('open_day') or '-'}")
    if not positions:
        lines.append("  （空仓）")
    msg = "\n".join(lines)
    pushed = feishu.send_text(msg) if push else False
    db.set_meta("postclose_last", json.dumps(now.isoformat()))

    return {"date": today, "saved": saved, "skipped": skipped,
            "positions": len(positions), "equity": round(equity, 2),
            "cash": round(cash, 2), "message": msg, "pushed": pushed,
            "last_run": now.isoformat()}
