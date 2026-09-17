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
from bisect import bisect_left
from collections import deque
from datetime import datetime
from typing import Optional

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


# ---------------- 平仓复盘（A+B：FIFO 配对批次 + 分类战绩 + 平仓后走势） ----------------

def _daily_close_seq(codes: list[str], data_dir: Optional[str] = None
                     ) -> dict[str, tuple[list[str], list[float]]]:
    """{code: (日期升序表, 收盘升序表)}——持有天数与平仓后走势共用一份日线。"""
    df = store.read_daily(codes=codes, data_dir=data_dir)
    seq: dict[str, tuple[list[str], list[float]]] = {}
    if df is None or not df.height:
        return seq
    for r in df.sort(["code", "date"]).iter_rows(named=True):
        dates, closes = seq.setdefault(r["code"], ([], []))
        dates.append(str(r["date"]))
        closes.append(float(r["close"]))
    return seq


def closed_trade_stats(data_dir: Optional[str] = None) -> dict:
    """平仓复盘（A+B）：回填流水 FIFO 配对 -> 已平仓批次 + 分类战绩 +
    平仓后 T+5/T+10 走势（清仓后下跌 = 卖对）。

    配对口径与回测 execute_sell 一致：一笔卖出从最老买入批次起 FIFO 吃进，
    跨批次时拆成多条配对记录（各保留来源批次的开仓日/开仓价/费用分摊）。
    当日买卖（做T）hold_days=0，自然单独成类。"""
    fills = list(reversed(db.list_live_fills(limit=100_000)))  # id 升序 = 回填顺序
    sig_map = {s["id"]: s for s in db.list_live_signals(limit=5000)}
    buys: dict[str, deque] = {}
    rows: list[dict] = []
    for f in fills:
        code = f["code"]
        if f["side"] == "buy":
            if int(f["fill_volume"]) > 0:
                buys.setdefault(code, deque()).append(f)
            continue
        rem = int(f["fill_volume"])
        if rem <= 0:
            continue
        sig = sig_map.get(f.get("signal_id")) or {}
        close_time = f.get("fill_time") or f.get("created_at") or ""
        close_day = close_time[:10]
        sell_fee_each = float(f["fee"] or 0) / rem
        while rem > 0 and buys.get(code):
            b = buys[code][0]
            b_vol = int(b["fill_volume"])
            take = min(rem, b_vol)
            buy_fee_part = float(b["fee"] or 0) * take / b_vol
            open_time = b.get("fill_time") or b.get("created_at") or ""
            cost = float(b["fill_price"]) * take
            pnl = ((float(f["fill_price"]) - float(b["fill_price"])) * take
                   - buy_fee_part - sell_fee_each * take)
            rows.append({
                "code": code,
                "name": (sig_map.get(b.get("signal_id")) or {}).get("name") or code,
                "open_day": open_time[:10], "close_day": close_day,
                "open_price": float(b["fill_price"]),
                "close_price": float(f["fill_price"]),
                "volume": take,
                "pnl": round(pnl, 2),
                "ret_pct": round(pnl / cost * 100, 4) if cost else None,
                "close_stype": sig.get("stype") or "",
                "close_reason": sig.get("reason") or "",
            })
            rem -= take
            b["fill_volume"] = b_vol - take
            if b["fill_volume"] <= 0:
                buys[code].popleft()

    # ---- 持有天数 + 平仓后 T+5/T+10 走势（日线缺失退 None，宁缺勿错） ----
    seq = _daily_close_seq(sorted({r["code"] for r in rows}), data_dir)
    for r in rows:
        dates, closes = seq.get(r["code"], ([], []))
        r["hold_days"] = None
        r["ret_after_5d"] = None
        r["ret_after_10d"] = None
        if not dates:
            continue
        i_open = bisect_left(dates, r["open_day"])
        i_close = bisect_left(dates, r["close_day"])
        if i_close >= len(dates) or dates[i_close] != r["close_day"]:
            continue   # 平仓日无日线（未更新到当日）：走势无从算起
        if i_open < len(dates):
            r["hold_days"] = max(0, i_close - i_open)
        for tag, n in (("ret_after_5d", 5), ("ret_after_10d", 10)):
            j = i_close + n
            if j < len(closes):
                r[tag] = round((closes[j] / r["close_price"] - 1) * 100, 4)
    rows.sort(key=lambda r: r["close_day"], reverse=True)

    # ---- 分类（做T=当日买卖优先；其余按卖出信号类型）+ 战绩汇总 ----
    for r in rows:
        r["kind"] = "做T" if r["hold_days"] == 0 else (r["close_stype"] or "未分类")
    by_kind: dict[str, dict] = {}
    rets_all = [r["ret_pct"] for r in rows if r["ret_pct"] is not None]
    t5 = [r["ret_after_5d"] for r in rows if r["ret_after_5d"] is not None]
    t10 = [r["ret_after_10d"] for r in rows if r["ret_after_10d"] is not None]

    def _pack(rs: list[dict]) -> dict:
        rets = [r["ret_pct"] for r in rs if r["ret_pct"] is not None]
        return {"n": len(rs),
                "total_pnl": round(sum(r["pnl"] for r in rs), 2),
                "win_rate": (round(sum(1 for r in rs if r["pnl"] > 0) / len(rs), 4)
                             if rs else None),
                "avg_ret_pct": (round(sum(rets) / len(rets), 4) if rets else None)}

    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    return {
        "rows": rows,
        "summary": {"n": len(rows),
                    "total_pnl": round(sum(r["pnl"] for r in rows), 2),
                    "win_rate": (round(sum(1 for r in rows if r["pnl"] > 0) / len(rows), 4)
                                 if rows else None),
                    "avg_ret_pct": (round(sum(rets_all) / len(rets_all), 4)
                                    if rets_all else None),
                    # 卖对率：平仓后 5/10 日收盘低于平仓价的批次占比（跌=躲过下跌）
                    "sell_right_rate_5d": (round(sum(1 for v in t5 if v < 0) / len(t5), 4)
                                           if t5 else None),
                    "sell_right_rate_10d": (round(sum(1 for v in t10 if v < 0) / len(t10), 4)
                                            if t10 else None)},
        "by_kind": {k: _pack(v) for k, v in sorted(by_kind.items())},
    }
