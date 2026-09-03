# -*- coding: utf-8 -*-
"""盘中信号机（LIVE_SIGNAL_SYSTEM §5 盘中，阶段 M2）。

每根完成 bar → SlotStepper 步进（与回测 momentum_slot._walk 同一实现）→
信号 → 风控前置过滤（T+1/槽位/预算上限）→ 飞书推送 + 落库 + 状态持久化。

与回测严格同口径的三个关键点：
1. 日线特征全部为 T-1 对齐（as_of = 日线库最新交易日），实时 bar 只提供 close
   ——与回测 prepare 的 feats_t1 join 语义一致（当日 bar 只能看见昨收特征）。
2. 特征在后复权空间计算，实时 bar 为原始价：把水平量（dif/dea/ma_fast/slope）
   ÷ as_of 复权因子换回原始空间。MACD/均线/斜率对价格线性缩放，所有比较
   （金叉/站上均线/阈值）与后复权空间逐项等价；atr_pct/bias/score/breakout
   本身缩放不变。
3. entry_allowed（今日可建仓名单）= as_of 日全市场动量分前 pool_n
   （rank_days 的 T-1 语义：昨日排名决定今日可建仓），不施加选股门槛。

风控前置（§9）：T+1 提示过滤（当日买入不发当日卖出）、max_holdings 槽位、
buy_budget 预算上限（对齐 risk.py 默认 max_position_pct_per_stock=40）、
数据断流熔断（盘中全源失败 >10 分钟推送告警）。
"""
import json
from datetime import datetime
from typing import Optional

import polars as pl

from .. import db
from ..data import store
from ..engine import momentum_core as mc
from ..engine.datafeed import _attach_adj
from ..engine.runner import _shift_back
from ..engine.strategies.momentum_slot import MomentumSlotStrategy, SlotStepper
from . import feishu, quotes

# 单票市值上限（占虚拟权益 %）——对齐 engine/risk.py 默认
MAX_POS_PCT = 40.0
CASH_RESERVE_PCT = 1.5     # 现金缓冲（%），对齐 risk.py 默认
CIRCUIT_BREAK_MIN = 10     # 断流熔断：全源失败持续分钟数

_MF_CACHE: dict = {}       # 全市场特征按日缓存（盘前/盘中共用一次计算）
_CODE_FEATS_CACHE: dict = {}   # 单股全历史特征按 (day, code) 缓存


def _live_cfg() -> dict:
    from .premarket import DEFAULT_CFG
    return {**DEFAULT_CFG, **db.get_live_config()}


def _stepper_params(cfg: dict) -> dict:
    """SlotStepper 参数：momentum_slot 参数表默认值 + 实盘可配置项覆盖。"""
    p = {k["key"]: k["default"] for k in MomentumSlotStrategy.param_schema}
    p.update({
        "t_mode": str(cfg.get("t_mode") or "off"),          # 做T机制（M2 起步 off）
        "exit_need": int(cfg.get("exit_need") or 2),        # 衰退信号满足数
        "pool_n": int(cfg.get("pool_n") or 6),              # 榜单容量
    })
    return p


def _feats_params(cfg: dict) -> dict:
    return mc.pick_params(above_ma=int(cfg["above_ma"]),
                          with_accel=bool(cfg["with_accel"]))


def market_features_cached(cfg: dict, data_dir=None) -> mc.MarketFeatures:
    """全市场日线特征（280 自然日窗口，与盘前 premarket 同口径），按日缓存。"""
    day = datetime.now().strftime("%Y-%m-%d")
    key = (day, int(cfg["above_ma"]), bool(cfg["with_accel"]))
    if _MF_CACHE.get("key") == key:
        return _MF_CACHE["mf"]
    mf = mc.market_features(
        data_dir=data_dir, window_start=_shift_back(day, 280),
        p=mc.pick_params(above_ma=int(cfg["above_ma"]),
                         with_accel=bool(cfg["with_accel"])))
    _MF_CACHE.clear()
    _MF_CACHE.update({"key": key, "mf": mf})
    return mf


def _code_features(code: str, p: dict, data_dir=None):
    """单股全历史日线特征（后复权，momentum_core 同口径）。

    返回 (特征表, 复权因子map{day:f}, 原始收盘map{day:c})；无数据返回 (None, {}, {})。
    day_idx 为全历史行号（跨日稳定的冷却期计数锚，回测同义）。"""
    key = (datetime.now().strftime("%Y-%m-%d"), code, str(data_dir))
    hit = _CODE_FEATS_CACHE.get(key)
    if hit is not None:
        return hit
    daily = store.read_daily([code], data_dir)
    if daily is None or daily.height == 0:
        return None, {}, {}
    daily = daily.select(["code", "date", "open", "high", "low", "close"])
    raw_close = dict(zip(daily["date"].str.slice(0, 10).to_list(),
                         [float(x) for x in daily["close"].to_list()]))
    adj = store.read_adj_factor([code], data_dir)
    daily = _attach_adj(daily.sort(["code", "date"]), adj)
    fac = dict(zip(daily["date"].str.slice(0, 10).to_list(),
                   [float(x) for x in daily["adj_factor"].to_list()]))
    g = daily.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
    g = g.rename({"close": "d_close", "high": "d_high", "low": "d_low"})
    f = mc.daily_feature_core(g, p, anchor_key="anchor_n", anchor_name="ma_fast",
                              with_accel=bool(p.get("with_accel")))
    out = (f, fac, raw_close)
    _CODE_FEATS_CACHE[key] = out
    return out


def _entry_allowed(mf: mc.MarketFeatures, as_of: str, pool_n: int) -> set:
    """今日可建仓名单 = as_of 全市场动量分前 pool_n（rank_days T-1 语义，无门槛）"""
    d = mf.feats.filter((pl.col("day") == as_of) & pl.col("score").is_not_null())
    if not d.height:
        return set()
    return set(d.sort("score", descending=True).head(max(1, pool_n))["code"].to_list())


def _virtual_equity(cfg: dict, positions: list[dict],
                    prices: dict[str, float]) -> tuple[float, float]:
    """(虚拟权益, 可用现金)：cash = 初始资金 + Σ卖出 − Σ买入 − 费用；
    mv = Σ 持仓股数 × 现价（无现价退成本价）"""
    cash = float(cfg["initial_capital"])
    for f in db.list_live_fills(limit=1_000_000):
        amt = float(f["fill_price"]) * int(f["fill_volume"])
        cash += (amt - float(f["fee"] or 0)) if f["side"] == "sell" \
            else -(amt + float(f["fee"] or 0))
    mv = sum(int(p["volume"]) * prices.get(p["code"], float(p["cost_price"]))
             for p in positions)
    return cash + mv, cash


def _persist_prices(positions: dict, qt_map: dict, now: datetime,
                    force: bool = False) -> None:
    """持仓现价快照落库（供前端持仓卡浮盈展示）。
    同一分钟内不重复写（控制台快照高频调用节流）；force=True 用于主动轮询。"""
    minute = now.strftime("%Y-%m-%d %H:%M")
    if not force and db.get_meta("pos_px_ts") == minute:
        return
    hit = False
    for c, q in qt_map.items():
        if c in positions and q.get("price"):
            db.update_live_position_price(c, float(q["price"]),
                                          now.isoformat(timespec="seconds"))
            hit = True
    if hit:
        db.set_meta("pos_px_ts", minute)


def _in_session(now: datetime) -> bool:
    """A股盘中时段（含集合竞价缓冲）：工作日 09:15~15:05"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


def drawdown_breaker(cfg: dict, equity: float, push: bool = True) -> tuple[Optional[str], bool]:
    """回撤熔断（§9，对齐回测 max_drawdown_breaker 默认 30%）：虚拟权益较
    峰值回撤 ≥ dd_breaker_pct% -> 强制 gate 停开仓 + 一次性飞书告警。
    峰值存 sig_meta(live_equity_peak)；清空重来清 KV 即自然复位。
    返回 (告警文案|None, 是否本次强制 gate)。"""
    if equity <= 0:
        return None, False
    peak = float(db.get_meta("live_equity_peak") or 0)
    if equity > peak:
        db.set_meta("live_equity_peak", f"{equity:.2f}")
        peak = equity
    if peak <= 0:
        return None, False
    dd = (1 - equity / peak) * 100
    limit = float(cfg.get("dd_breaker_pct") or 30)
    if dd < limit:
        return None, False
    pool = db.get_live_pool()
    if pool and not pool.get("gate_state"):
        db.save_live_pool(pool.get("pool") or [], pool.get("as_of"), 1,
                          pool.get("health_history") or [],
                          pool.get("idle_start"))
    if not db.get_meta("dd_breaker_alerted"):
        db.set_meta("dd_breaker_alerted", "1")
        msg = (f"【⚠ 回撤熔断】虚拟权益 {equity:,.0f} 较峰值 {peak:,.0f} "
               f"回撤 {dd:.1f}%（≥{limit:.0f}%）——已强制停开仓，存量持仓的"
               f"退出/止损信号照常；解除请清空重来或上调 dd_breaker_pct")
        if push:
            feishu.send_text(msg)
        return msg, True
    return "回撤熔断中（此前已告警）", True


def _stype_of(sig: dict) -> str:
    """状态机 tag -> 实盘信号类型（tag 空串 = 衰退清仓二次）"""
    tag = sig["tag"]
    if tag:
        return tag
    return "清仓" if sig["signal"] < 0 else "加仓"


def run_intraday(data_dir=None, push: bool = True,
                 now: Optional[datetime] = None) -> dict:
    """执行一次盘中轮询：拉行情 → 喂完成 bar → 信号 → 风控 → 推送/落库。

    幂等：last_bar 游标去重，同一 bar 不重复喂；可安全反复调用。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    cfg = _live_cfg()
    p_stepper = _stepper_params(cfg)
    p_feats = _feats_params(cfg)

    pool_state = db.get_live_pool()
    positions = {p["code"]: p for p in db.list_live_positions()}
    states = db.get_strategy_states()
    pool_codes = [x["code"] for x in (pool_state.get("pool") or [])]
    active = sorted(set(pool_codes) | set(positions) | set(states))
    if not active:
        return {"skipped": "无池子/持仓/状态——请先执行盘前流程",
                "signals": [], "suspended": [], "notes": [], "fed_bars": 0}

    mf = market_features_cached(cfg, data_dir)
    as_of = pool_state.get("as_of") or mf.calendar[-1]
    if as_of not in set(mf.calendar):
        as_of = mf.calendar[-1]
    gate_state = int(pool_state.get("gate_state") or 0)
    entry_allowed = _entry_allowed(mf, as_of, int(cfg["pool_n"]))

    name_map: dict[str, str] = {}
    try:
        basic = store.read_stock_basic(data_dir)
        if basic is not None and basic.height:
            name_map = {r["code"]: r["name"]
                        for r in basic.select(["code", "name"]).to_dicts()
                        if r.get("name")}
    except Exception:
        pass

    qt_map = quotes.realtime_quotes(active)
    _persist_prices(positions, qt_map, now, force=True)
    equity, cash = _virtual_equity(
        cfg, list(positions.values()),
        {c: q["price"] for c, q in qt_map.items()})
    # 回撤熔断（§9）：在 gate 判定前执行——触发则本轮起强制停开仓
    dd_msg, dd_forced = drawdown_breaker(cfg, equity, push=push)
    if dd_forced:
        gate_state = 1

    signals: list[dict] = []
    suspended: list[dict] = []
    notes: list[str] = []
    no_data: list[str] = []
    fed_bars = 0
    any_bars = False

    for code in active:
        bars = quotes.fetch_minute5(code, today)
        if bars is None or bars.height == 0:
            no_data.append(code)
            continue
        done = quotes.completed_bars(bars, now)
        if not done.height:
            no_data.append(code)
            continue
        any_bars = True
        qt = qt_map.get(code)

        f, fac, raw_close = _code_features(code, p_feats, data_dir)
        if f is None:
            no_data.append(code)
            continue
        row = f.filter(pl.col("day") == as_of)
        if not row.height:
            no_data.append(code)
            continue
        r = row.to_dicts()[0]
        # 除权检测（§4.3）：只提示不发交易信号（状态机冻结，游标不推进）
        adj_warn = quotes.check_adj_mismatch(qt, raw_close.get(as_of))
        if adj_warn:
            suspended.append({"code": code, "reason": adj_warn})
            continue
        if fac.get(as_of) in (None, 0):
            no_data.append(code)
            continue

        # T-1 特征换回原始价空间（÷ as_of 复权因子），与实时 bar 同空间
        f_asof = float(fac[as_of])

        def _lvl(v):
            return None if v is None else float(v) / f_asof

        feats_row = {"atr_pct": r.get("atr_pct"), "bias": r.get("bias"),
                     "vol_pos": r.get("vol_pos"), "breakout": r.get("breakout"),
                     "score": r.get("score"),
                     "dif": _lvl(r.get("dif")), "dea": _lvl(r.get("dea")),
                     "ma_fast": _lvl(r.get("ma_fast")), "slope": _lvl(r.get("slope")),
                     "day_idx": int(r.get("day_idx") or 0)}

        saved = states.get(code) or {}
        stepper = SlotStepper(p_stepper, {today} if code in entry_allowed else set())
        stepper.restore(saved.get("st") or {})
        last_bar = saved.get("last_bar")

        # 交叉校验（§4.3）前置：偏离 >1% 先双源同刻复核（方案A）——
        # 复核一致=真实急拉放行；不一致/无法验证=坏数据暂停（游标不推进）
        if qt:
            div = quotes.check_bar_divergence(float(done["close"][-1]), qt)
            if div:
                verdict = quotes.cross_check_bar(
                    code, done["date"][-1], float(done["close"][-1]))
                if verdict:
                    suspended.append({"code": code,
                                      "reason": verdict + "——本轮暂停该票信号"})
                    continue
                notes.append(f"{code} {done['date'][-1]} 偏离{div.split('偏离')[1]}，"
                             f"双源同刻复核一致（真实急拉），放行")

        new_bars = (done.filter(pl.col("date") > last_bar)
                    if last_bar else done)

        sigs_code: list[tuple[str, dict, float]] = []
        for br in new_bars.iter_rows(named=True):
            sig = stepper.step(br["date"], float(br["close"]),
                               feats_row["atr_pct"], feats_row["bias"],
                               feats_row["vol_pos"], feats_row["breakout"],
                               feats_row["dif"], feats_row["dea"],
                               feats_row["ma_fast"], feats_row["slope"],
                               feats_row["score"], feats_row["day_idx"],
                               bool(gate_state),
                               br["date"][11:16] == "15:00")
            if sig is not None:
                sigs_code.append((br["date"], sig, float(br["close"])))

        for bar_ts, sig, close in sigs_code:
            item = _make_signal(sig, code, name_map.get(code, code), bar_ts, close,
                                cfg, positions, equity, cash, today)
            if item.get("blocked"):
                suspended.append({"code": code, "reason": item["blocked"]})
                continue
            sid = db.add_live_signal(
                "intraday", item["stype"], code, item["name"], sig["reason"],
                item.get("amount"), close,
                extra={"bar": bar_ts, "budget_pct": sig.get("budget_pct"),
                       "t_ratio": sig.get("t_ratio"),
                       "reduce_pct": sig.get("reduce_pct"), "equity": round(equity, 2)})
            signals.append({"id": sid, "code": code, "stype": item["stype"],
                            "name": item["name"], "reason": sig["reason"],
                            "suggest_amount": item.get("amount"),
                            "ref_price": close, "bar": bar_ts})

        if new_bars.height:
            db.save_strategy_state(code, stepper.state(), new_bars["date"][-1])
            fed_bars += new_bars.height

    _circuit_check(any_bars, now, push)

    pushed = False
    if push and (signals or suspended):
        pushed = feishu.send_text(_compose_message(now, signals, suspended,
                                                   no_data, equity, cash))
    return {"as_of": as_of, "signals": signals, "suspended": suspended,
            "notes": notes, "no_data": no_data, "fed_bars": fed_bars,
            "equity": round(equity, 2), "cash": round(cash, 2),
            "dd_warning": dd_msg,
            "message": _compose_message(now, signals, suspended, no_data,
                                        equity, cash) if (signals or suspended) else "",
            "pushed": pushed}


def _make_signal(sig: dict, code: str, name: str, bar_ts: str, close: float,
                 cfg: dict, positions: dict, equity: float, cash: float,
                 today: str) -> dict:
    """信号 -> 实盘信号字段 + 风控前置（T+1/槽位/预算）。blocked 非空 = 拦截。"""
    stype = _stype_of(sig)
    pos = positions.get(code)
    amount: Optional[float] = None
    blocked = ""
    sell_side = sig["signal"] < 0

    if sell_side:
        # T+1（§9）：当日买入的票不发当日卖出信号
        if pos and (pos.get("open_day") or "") >= today:
            blocked = f"{code} T+1：当日买入不可卖，卖出信号已拦截"
        elif stype == "减仓" and pos:
            amount = round(pos["volume"] * close * float(sig.get("reduce_pct") or 0) / 100, 0)
        elif stype == "做T" and pos:
            amount = round(pos["volume"] * close * float(sig.get("t_ratio") or 0) / 100, 0)
        # 止损/清仓：全仓，amount=None
    else:
        if stype == "开仓":
            if pos:
                blocked = f"{code} 已有持仓（盘前/此前信号已建仓），开仓信号去重拦截"
            elif len(positions) >= int(cfg.get("max_holdings") or 3):
                blocked = (f"槽位已满（max_holdings={cfg.get('max_holdings')}），"
                           f"{code} 开仓信号拦截")
            elif any(s["code"] == code and s["stype"] == "开仓"
                     and s["status"] == "待执行"
                     and (s.get("ts") or "")[:10] == today
                     for s in db.list_live_signals(limit=200)):
                blocked = (f"{code} 今日已有待执行开仓信号（盘前名单），"
                           f"盘中开仓去重拦截")
        if not blocked:
            budget_pct = float(sig.get("budget_pct") or 0)
            amount = equity * budget_pct / 100
            mv_code = (pos["volume"] * close) if pos else 0.0
            amount = min(amount, cash * (1 - CASH_RESERVE_PCT / 100),
                         max(0.0, equity * MAX_POS_PCT / 100 - mv_code))
            amount = round(amount, 0)
            if amount < close * 100:
                blocked = (f"{code} 预算不足一手（预算 {amount:.0f} 元 < "
                           f"100股×{close}），信号拦截")
                amount = None
    return {"stype": stype, "name": name, "amount": amount, "blocked": blocked}


def _compose_message(now: datetime, signals: list[dict], suspended: list[dict],
                     no_data: list[str], equity: float, cash: float) -> str:
    lines = [f"【盘中信号 {now.strftime('%H:%M')}】"
             f"权益 {equity:,.0f}｜可用 {cash:,.0f}"]
    for s in signals:
        amt = f"｜建议金额 {s['suggest_amount']:,.0f}元" \
            if s.get("suggest_amount") else ""
        lines.append(f"● {s['code']} {s['name']}｜{s['stype']}｜{s['reason']}"
                     f"{amt}｜参考价 {s['ref_price']}")
    for w in suspended:
        lines.append(f"⚠ {w['code']}：{w['reason']}")
    if no_data:
        lines.append(f"（无行情数据：{', '.join(no_data)}）")
    if not signals and not suspended and not no_data:
        lines.append("本轮无信号")
    return "\n".join(lines)


def _circuit_check(any_bars: bool, now: datetime, push: bool) -> None:
    """断流熔断（§9）：盘中全源失败 >10 分钟 → 推送告警一次；恢复后复位。
    心跳在每轮有数据时刷新（否则长会话后的短故障会提前误报）。"""
    if not _in_session(now):
        return
    try:
        hb = json.loads(db.get_meta("intraday_hb") or "{}")
    except Exception:
        hb = {}
    if any_bars:
        if hb.get("alerted") and push:
            feishu.send_text("【盘中行情恢复】各源已恢复出数，信号机继续运行")
        db.set_meta("intraday_hb", json.dumps({"ok_ts": now.isoformat(),
                                               "alerted": False}))
        return
    ok_ts = hb.get("ok_ts")
    if not ok_ts:
        db.set_meta("intraday_hb", json.dumps({"ok_ts": now.isoformat(),
                                               "alerted": False}))
        return
    down_min = (now - datetime.fromisoformat(ok_ts)).total_seconds() / 60
    if down_min >= CIRCUIT_BREAK_MIN and not hb.get("alerted"):
        if push:
            feishu.send_text(
                f"【⚠ 断流熔断】盘中实时行情全部源失败已 {down_min:.0f} 分钟"
                f"（> {CIRCUIT_BREAK_MIN} 分钟）——状态机冻结不推进，"
                f"请检查网络/行情源")
        db.set_meta("intraday_hb", json.dumps({"ok_ts": ok_ts, "alerted": True}))


def status_snapshot(data_dir=None) -> dict:
    """盘中控制台快照（轻量：qt 批量报价 + 状态/持仓，不拉 K 线）"""
    cfg = _live_cfg()
    pool_state = db.get_live_pool()
    positions = {p["code"]: p for p in db.list_live_positions()}
    states = db.get_strategy_states()
    pool_codes = [x["code"] for x in (pool_state.get("pool") or [])]
    active = sorted(set(pool_codes) | set(positions) | set(states))
    qt_map = quotes.realtime_quotes(active)
    _persist_prices(positions, qt_map, datetime.now())
    try:
        hb = json.loads(db.get_meta("intraday_hb") or "{}")
    except Exception:
        hb = {}
    codes = []
    for c in active:
        st = (states.get(c) or {}).get("st") or {}
        codes.append({
            "code": c, "name": (qt_map.get(c) or {}).get("name") or
            next((p["name"] for p in positions.values() if p["code"] == c), c),
            "price": (qt_map.get(c) or {}).get("price"),
            "prev_close": (qt_map.get(c) or {}).get("prev_close"),
            "held": c in positions,
            "opened": bool(st.get("opened")), "full": bool(st.get("full")),
            "adds_done": st.get("adds_done") or 0,
            "exit_stage": st.get("exit_stage") or 0,
            "last_bar": (states.get(c) or {}).get("last_bar"),
            "in_pool": c in pool_codes,
        })
    return {"session": _in_session(datetime.now()),
            "as_of": pool_state.get("as_of"),
            "gate_state": int(pool_state.get("gate_state") or 0),
            "codes": codes, "heartbeat": hb,
            "t_mode": str(cfg.get("t_mode") or "off")}
