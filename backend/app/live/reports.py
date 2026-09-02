# -*- coding: utf-8 -*-
"""M3 影子运行统计 / 滑点统计 / M4 小资金实盘就绪检查（LIVE_SIGNAL_SYSTEM §8/§10）。

- 滑点统计：sig_fills 的实际成交价 vs 关联信号参考价（信号发出时点的合理
  预期成交价），按方向折算成"滑点成本"（买贵/卖便宜为正损失），月度汇总
  反哺回测滑点模型。
- 影子运行：假想"每条信号都按参考价足额执行"的影子账户（FIFO 已实现盈亏）
  vs 实际回填成交的已实现盈亏——差值 = 漏执行 + 执行延迟 + 滑点的合计代价。
  影子账户做T信号不计（t_mode=off 起步）。
- 就绪检查：M4 小资金跟单前的硬条件清单（数据/通道/影子时长/滑点样本）。
"""
from collections import deque
from datetime import datetime

from .. import db
from ..data import sources, store
from . import feishu, quotes
from .premarket import DEFAULT_CFG

# 参与影子账户的信号类型
_TRADE_STYPES = ("开仓", "加仓", "减仓", "止损", "清仓")


def slippage_stats(limit: int = 500) -> dict:
    """滑点流水 + 汇总。slip_cost：买入(fill-ref)/ref、卖出(ref-fill)/ref，
    正值 = 对用户不利的滑点成本。"""
    fills = db.list_live_fills(limit=limit)
    sig_map = {s["id"]: s for s in db.list_live_signals(limit=2000)}
    rows = []
    for f in fills:
        sig = sig_map.get(f.get("signal_id"))
        if not sig or sig.get("ref_price") in (None, 0):
            continue
        ref = float(sig["ref_price"])
        raw = (float(f["fill_price"]) - ref) / ref
        cost = raw if f["side"] == "buy" else -raw
        rows.append({
            "fill_id": f["id"], "signal_id": f["signal_id"],
            "code": f["code"], "name": sig.get("name") or "",
            "stype": sig.get("stype") or "", "side": f["side"],
            "ref_price": ref, "fill_price": float(f["fill_price"]),
            "fill_volume": f["fill_volume"],
            "slip_pct": round(cost * 100, 4),
            "fill_time": f.get("fill_time") or f.get("created_at"),
        })
    buys = [r["slip_pct"] for r in rows if r["side"] == "buy"]
    sells = [r["slip_pct"] for r in rows if r["side"] == "sell"]
    allc = [r["slip_pct"] for r in rows]

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {"rows": rows,
            "summary": {"n": len(rows), "avg_slip_pct": _avg(allc),
                        "buy_avg_slip_pct": _avg(buys),
                        "sell_avg_slip_pct": _avg(sells),
                        "worst_slip_pct": max(allc) if allc else None}}


def _realize(events: list[tuple[str, float, float]]) -> float:
    """FIFO 已实现盈亏。events 按时间序：(side, price, volume)"""
    q: deque[list[float]] = deque()
    pnl = 0.0
    for side, price, vol in events:
        if side == "buy":
            if vol > 0:
                q.append([price, vol])
            continue
        remain = vol
        while remain > 1e-9 and q:
            p, v = q[0]
            take = min(v, remain)
            pnl += (price - p) * take
            remain -= take
            if take >= v - 1e-9:
                q.popleft()
            else:
                q[0][1] = v - take
    return pnl


def shadow_stats() -> dict:
    """影子运行统计：信号执行率 + 影子账户（全部按参考价足额执行）vs 实际回填。"""
    signals = [s for s in db.list_live_signals(limit=5000)
               if s.get("code") and s["stype"] in _TRADE_STYPES]
    signals.sort(key=lambda s: s["id"])
    filled = sum(1 for s in signals if s["status"] == "已成交")
    ignored = sum(1 for s in signals if s["status"] in ("已忽略", "已过期"))

    # ---- 影子账户：每条信号按 ref_price 足额执行（FIFO） ----
    shadow_events: dict[str, list[tuple[str, float, float]]] = {}
    open_shares: dict[str, float] = {}
    for s in signals:
        code = s["code"]
        ref = s.get("ref_price")
        if not ref:
            continue
        ref = float(ref)
        ev = shadow_events.setdefault(code, [])
        if s["stype"] in ("开仓", "加仓"):
            amt = s.get("suggest_amount")
            if not amt:
                continue
            vol = float(amt) / ref
            ev.append(("buy", ref, vol))
            open_shares[code] = open_shares.get(code, 0.0) + vol
        elif s["stype"] in ("清仓", "止损"):
            vol = open_shares.get(code, 0.0)
            if vol > 0:
                ev.append(("sell", ref, vol))
                open_shares[code] = 0.0
        elif s["stype"] == "减仓":
            vol = open_shares.get(code, 0.0)
            pct = float((s.get("extra") or {}).get("reduce_pct") or 0) / 100
            if vol > 0 and pct > 0:
                sell = vol * pct
                ev.append(("sell", ref, sell))
                open_shares[code] = vol - sell
    shadow_pnl = sum(_realize(ev) for ev in shadow_events.values())

    # ---- 实际口径：回填成交 FIFO ----
    actual_events: dict[str, list[tuple[str, float, float]]] = {}
    for f in sorted(db.list_live_fills(limit=100000), key=lambda x: x["id"]):
        actual_events.setdefault(f["code"], []).append(
            (f["side"], float(f["fill_price"]), float(f["fill_volume"])))
    actual_pnl = sum(_realize(ev) for ev in actual_events.values())

    days = len({(s.get("ts") or "")[:10] for s in signals} - {""})
    return {
        "n_signals": len(signals), "n_filled": filled, "n_ignored": ignored,
        "fill_rate": round(filled / len(signals), 4) if signals else None,
        "shadow_pnl": round(shadow_pnl, 2),
        "actual_pnl": round(actual_pnl, 2),
        "gap_pnl": round(actual_pnl - shadow_pnl, 2),
        "days": days,
    }


def readiness() -> dict:
    """M4 小资金实盘跟单就绪清单（§10：M3 影子 2-4 周无数据事故后再上）"""
    cfg = {**__import__("app.live.premarket", fromlist=["DEFAULT_CFG"]).DEFAULT_CFG,
           **db.get_live_config()}
    items: list[dict] = []

    def _add(key, label, ok, detail):
        items.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    _add("feishu", "飞书推送已配置", feishu.configured(),
         "信号只在推送通道可靠时才可跟单（盘中人不在电脑前）")

    pool = db.get_live_pool()
    as_of = pool.get("as_of")
    fresh = False
    detail = f"基准日 {as_of or '无'}"
    if as_of:
        lag = (datetime.now() - datetime.strptime(as_of, "%Y-%m-%d")).days
        fresh = lag <= 4
        detail += f"（滞后 {lag} 天）"
    _add("data_fresh", "日线数据新鲜（滞后≤4天）", fresh, detail)

    n_codes = 0
    try:
        d = store.read_daily(None)
        n_codes = d["code"].n_unique() if d is not None and d.height else 0
    except Exception:
        pass
    _add("daily_coverage", "日线覆盖完整（≥4000只）", n_codes >= 4000,
         f"当前 {n_codes} 只（覆盖不足=幸存者偏差）")

    probes = {"mootdx": False, "sina": False, "qt": False}
    for s in ("mootdx", "sina"):
        src = next((x for x in sources.SOURCES if x.name == s), None)
        if src is not None and src.available():
            try:
                probes[s] = bool(src.health_check(timeout=6))
            except Exception:
                pass
    try:
        probes["qt"] = bool(quotes.realtime_quotes(["600000"], timeout=4))
    except Exception:
        pass
    _add("quotes", "盘中行情源可用（mootdx/新浪/qt）",
         probes["qt"] and (probes["mootdx"] or probes["sina"]),
         f"mootdx={probes['mootdx']} sina={probes['sina']} qt={probes['qt']}")

    t_off = str(cfg.get("t_mode") or "off") == "off"
    _add("t_mode_off", "做T机制关闭（t_mode=off）", t_off,
         "5分钟做T人工执行延迟会显著吃掉收益，主逻辑验证前建议关闭")

    sh = shadow_stats()
    _add("shadow_days", "影子运行 ≥5 个交易日", sh["days"] >= 5,
         f"已积累 {sh['days']} 天信号；当前执行率 "
         f"{sh['fill_rate'] * 100 if sh['fill_rate'] is not None else '-'}%")

    slip = slippage_stats()
    _add("slippage_n", "滑点样本 ≥10 笔", slip["summary"]["n"] >= 10,
         f"当前 {slip['summary']['n']} 笔，平均滑点成本 "
         f"{slip['summary']['avg_slip_pct']}%")

    mh = int(cfg.get("max_holdings") or 3)
    _add("small_capital", "max_holdings ≤5（小资金灰度建议）", 0 < mh <= 5,
         f"当前 {mh}——小资金阶段控制同时持仓只数")

    return {"ready": all(i["ok"] for i in items), "items": items}
