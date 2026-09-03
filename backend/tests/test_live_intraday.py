# -*- coding: utf-8 -*-
"""实盘盘中信号机测试（LIVE_SIGNAL_SYSTEM M2/M3）：
SlotStepper 状态恢复连续性 / 完成 bar 语义 / 盘中轮询游标幂等 /
盘后分钟线合并落库 / 影子账户 FIFO 与滑点统计。
行情一律 monkeypatch（不触网）；run_* 一律 push=False（.env 有真实 webhook）。
"""
import datetime as dt
import json

import numpy as np
import polars as pl
import pytest

from app import db
from app.data import store, synthetic
from app.engine.strategies.momentum_slot import MomentumSlotStrategy, SlotStepper
from app.live import intraday, postclose, premarket, quotes, reports
from test_live_signal import _write_market

TODAY = "2026-09-03"


@pytest.fixture(autouse=True)
def _sig_env():
    """meta.db 建信号机表（幂等）+ 测试后清空 sig_* 行（防污染真实库）"""
    db.init_db()
    yield
    with db.conn() as c:
        for t in ("sig_signal_log", "sig_fills", "sig_position", "sig_pool",
                  "sig_config", "sig_t_debt", "sig_withdraw",
                  "sig_strategy_state", "sig_meta"):
            c.execute(f"DELETE FROM {t}")


# ---------------- SlotStepper：状态恢复连续性（步进化核心保证） ----------------

def _minute_df(code, dates, rets, start_i=40, bars_per_day=4):
    """合成 5 分钟线：每日 4 根（09:35..09:50），日内均匀分解日收益 + 噪声"""
    rng = np.random.default_rng(7)
    rows = []
    prev = 10.0
    times = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00"][:bars_per_day]
    for di, d in enumerate(dates):
        if di < start_i:
            continue
        for hhmm in times:
            c = prev * (1 + rets.get(di, 0.0) / bars_per_day
                        + float(rng.normal(0, 0.008)))
            rows.append({"code": code, "date": f"{d} {hhmm}",
                         "open": prev, "high": max(prev, c) * 1.001,
                         "low": min(prev, c) * 0.999, "close": c,
                         "volume": 1000.0, "amount": c * 1000})
            prev = c
    return pl.DataFrame(rows)


def _joined(df, p, pool_n=6):
    """复刻 prepare 的特征 join（T-1 对齐 + pool_gate 兜底列）"""
    strat = MomentumSlotStrategy()
    feats = strat._daily_features(df, p)
    top_days = strat._rank_days({"600001": feats}, pool_n).get("600001", set())
    d = df.with_columns(pl.col("date").str.slice(0, 10).alias("day"))
    feats_t1 = feats.with_columns(
        pl.col("day").shift(-1).alias("day")).drop_nulls("day")
    d = (d.join(feats_t1, on="day", how="left")
         .with_columns(pl.lit(False).alias("pool_gate")))
    return d, top_days


SEL_COLS = ["date", "close", "atr_pct", "bias", "vol_pos", "breakout",
            "dif", "dea", "ma_fast", "slope", "score", "day_idx", "pool_gate"]


def test_stepper_restore_continuity():
    """半程快照 -> restore -> 续跑：与一次性跑完的信号序列完全一致"""
    dates = synthetic.trade_dates(340)
    rets = {i: (0.006 if i < 320 else -0.012) for i in range(len(dates))}
    df = _minute_df("600001", dates, rets)
    p = {k["key"]: k["default"] for k in MomentumSlotStrategy.param_schema}
    d, top_days = _joined(df, p)
    full = MomentumSlotStrategy._walk(d, p, top_days, None)
    sig_full, tag_full = full[0].to_list(), full[1].to_list()
    assert any(s != 0 for s in sig_full), "合成行情应产生信号"

    n = d.height
    split = n // 2 + 3
    dts = d["date"].to_list()
    is_eod = [i == n - 1 or dts[i][:10] != dts[i + 1][:10] for i in range(n)]
    rows = list(d.select(SEL_COLS).iter_rows())

    st1 = SlotStepper(p, top_days)
    for i in range(split):
        r = rows[i]
        st1.step(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                 r[9], r[10], r[11], bool(r[12]), is_eod[i])
    st2 = SlotStepper(p, top_days)
    st2.restore(st1.state())
    sig2, tag2 = [0] * n, [""] * n
    for i in range(split, n):
        r = rows[i]
        sig = st2.step(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                       r[9], r[10], r[11], bool(r[12]), is_eod[i])
        if sig:
            sig2[i], tag2[i] = sig["signal"], sig["tag"]
    assert sig2[split:] == sig_full[split:], "恢复后续跑信号应与整跑一致"
    assert tag2[split:] == tag_full[split:]
    assert sum(1 for s in sig_full[split:] if s != 0) >= 1, "尾段应有信号"


def test_stepper_walk_wrapper_matches_manual_feed():
    """_walk 包装层与手工逐 bar 喂入：输出列逐一一致（包装映射回归）"""
    dates = synthetic.trade_dates(340)
    rets = {i: (0.006 if i < 320 else -0.012) for i in range(len(dates))}
    df = _minute_df("600001", dates, rets)
    p = {k["key"]: k["default"] for k in MomentumSlotStrategy.param_schema}
    d, top_days = _joined(df, p)
    full = MomentumSlotStrategy._walk(d, p, top_days, None)

    n = d.height
    dts = d["date"].to_list()
    is_eod = [i == n - 1 or dts[i][:10] != dts[i + 1][:10] for i in range(n)]
    st = SlotStepper(p, top_days)
    sig2, tag2 = [0] * n, [""] * n
    for i, r in enumerate(d.select(SEL_COLS).iter_rows()):
        sig = st.step(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                      r[9], r[10], r[11], bool(r[12]), is_eod[i])
        if sig:
            sig2[i], tag2[i] = sig["signal"], sig["tag"]
    assert sig2 == full[0].to_list() and tag2 == full[1].to_list()


# ---------------- 完成 bar 语义（§4.4） ----------------

def test_completed_bars_drops_forming_bar():
    bars = pl.DataFrame({"date": [f"{TODAY} {t}" for t in
                                  ("09:35", "09:40", "09:45", "09:50")],
                         "close": [1.0, 2.0, 3.0, 4.0]})
    done = quotes.completed_bars(bars, dt.datetime(2026, 9, 3, 9, 41))
    assert done["date"].to_list() == [f"{TODAY} 09:35", f"{TODAY} 09:40"], \
        "进行中 bar（戳>now）不得喂状态机"
    done2 = quotes.completed_bars(bars, dt.datetime(2026, 9, 3, 15, 0))
    assert done2.height == 4


# ---------------- 数据校验闸门（§4.3）与断流熔断（§9） ----------------

def test_quote_guards_divergence_and_adj():
    """决策②③的直接验证：交叉校验 / 除权检测 / 缺数据放行"""
    qt = {"name": "股X", "price": 10.0, "prev_close": 10.0}
    assert quotes.check_bar_divergence(10.05, qt) is None, "偏离≤1% 放行"
    assert quotes.check_bar_divergence(10.2, qt) is not None, "偏离>1% 必须告警"
    assert quotes.check_adj_mismatch(qt, 10.0) is None, "昨收一致放行"
    assert quotes.check_adj_mismatch({"price": 9.0, "prev_close": 8.0},
                                     10.0) is not None, "昨收不一致（疑似除权）必须告警"
    assert quotes.check_bar_divergence(10.0, {}) is None, "无 qt 时不阻断正常流程"
    assert quotes.check_adj_mismatch(qt, None) is None, "无参考收盘时不阻断"


def test_circuit_breaker_states(tmp_path, monkeypatch):
    """断流熔断状态机：首次记心跳不告警 -> 超10分钟告警一次 -> 不重复 -> 恢复复位"""
    pushed: list[str] = []
    monkeypatch.setattr(intraday.feishu, "send_text",
                        lambda msg: pushed.append(msg) or True)
    now = dt.datetime(2026, 9, 3, 10, 30)   # 周三盘中
    # 首次无心跳：只记心跳，不告警
    intraday._circuit_check(False, now, push=True)
    assert not pushed
    # 心跳停在 11 分钟前：触发熔断告警恰好一次
    db.set_meta("intraday_hb", json.dumps(
        {"ok_ts": (now - dt.timedelta(minutes=11)).isoformat(),
         "alerted": False}))
    intraday._circuit_check(False, now, push=True)
    assert len(pushed) == 1 and "断流熔断" in pushed[0]
    # 已告警状态不重复推送
    intraday._circuit_check(False, now, push=True)
    assert len(pushed) == 1
    # 行情恢复：复位 + 推送恢复消息
    intraday._circuit_check(True, now, push=True)
    assert len(pushed) == 2 and "恢复" in pushed[1]
    assert json.loads(db.get_meta("intraday_hb"))["alerted"] is False


# ---------------- 盘中轮询：游标幂等 + 状态落库 ----------------

def test_intraday_flow_and_cursor(tmp_path, monkeypatch):
    dates = _write_market(tmp_path)
    db.save_live_config({"auto_idle_days": 5, "top_x": 2, "auto_index": [],
                         "auto_boards": [], "exit_need": 2,
                         "max_holdings": 3, "t_mode": "off"})
    r = premarket.run_premarket(data_dir=str(tmp_path), push=False)
    assert r["rebalanced"] and r["pool"]
    pool_codes = [p["code"] for p in r["pool"]]
    as_of = r["as_of"]
    daily = store.read_daily(None, str(tmp_path))
    last_close = {}
    for c in pool_codes:
        row = daily.filter((pl.col("code") == c) & (pl.col("date") == as_of))
        last_close[c] = float(row["close"][0])

    def fake_fetch(code, day):
        assert day == TODAY
        base = last_close[code]
        return pl.DataFrame([
            {"code": code, "date": f"{TODAY} 09:35", "open": base,
             "high": base * 1.01, "low": base * 0.99, "close": base * 1.005,
             "volume": 1e5, "amount": 1e8},
            {"code": code, "date": f"{TODAY} 09:40", "open": base * 1.005,
             "high": base * 1.02, "low": base, "close": base * 1.012,
             "volume": 1e5, "amount": 1e8},
            {"code": code, "date": f"{TODAY} 09:45", "open": base * 1.012,
             "high": base * 1.03, "low": base, "close": base * 1.02,
             "volume": 1e5, "amount": 1e8},
        ])

    monkeypatch.setattr(quotes, "fetch_minute5", fake_fetch)
    monkeypatch.setattr(quotes, "realtime_quotes",
                        lambda codes, timeout=5.0: {})
    now = dt.datetime(2026, 9, 3, 10, 0)

    out = intraday.run_intraday(data_dir=str(tmp_path), push=False, now=now)
    assert out["fed_bars"] == 3 * len(pool_codes)
    assert out["as_of"] == as_of
    assert out["equity"] == pytest.approx(3_000_000.0), "无回填时权益=初始资金"
    states = db.get_strategy_states()
    for c in pool_codes:
        assert states[c]["last_bar"] == f"{TODAY} 09:45", "游标应推进到最后完成 bar"
    intraday_sigs = [s for s in db.list_live_signals(limit=100)
                     if s["kind"] == "intraday"]
    # 盘前已对池内票发待执行开仓信号 -> 盘中同票开仓应被去重拦截（不重复推送）
    pool_set = set(pool_codes)
    assert not any(s["stype"] == "开仓" and s["code"] in pool_set
                   for s in intraday_sigs), \
        f"池内票盘中开仓应被去重拦截: {intraday_sigs}"
    assert any("已有待执行开仓信号" in w["reason"] for w in out["suspended"]), \
        f"去重拦截应出现在 suspended: {out['suspended']}"

    # 第二轮：同数据重复轮询 -> 游标去重零喂入、无重复信号
    n_before = len(db.list_live_signals(limit=100))
    out2 = intraday.run_intraday(data_dir=str(tmp_path), push=False, now=now)
    assert out2["fed_bars"] == 0
    assert len(db.list_live_signals(limit=100)) == n_before


def test_intraday_state_restore_add_signal(tmp_path, monkeypatch):
    """恢复 opened=True/full=False 状态后，斜率确认 -> 盘中『试仓升级』加仓信号"""
    dates = _write_market(tmp_path)
    db.save_live_config({"auto_idle_days": 5, "top_x": 2, "auto_index": [],
                         "auto_boards": [], "exit_need": 2,
                         "max_holdings": 3, "t_mode": "off"})
    premarket.run_premarket(data_dir=str(tmp_path), push=False)
    # 000001 属上升组：用真实收盘做 bar 基准（高于 MA20 -> 跌破均线信号恒假，
    # 退化为至多 1 个衰退信号 < exit_need=2，不会触发退出分支）
    daily = store.read_daily(None, str(tmp_path))
    row00 = daily.filter((pl.col("code") == "000001")
                         & (pl.col("date") == dates[-1]))
    base = float(row00["close"][0])
    db.save_strategy_state("000001", {"opened": 1, "full": 0}, None)

    def fake_fetch(code, day):
        return pl.DataFrame([
            {"code": code, "date": f"{TODAY} 09:35", "open": base,
             "high": base * 1.01, "low": base * 0.99, "close": base * 1.01,
             "volume": 1e5, "amount": 1e8},
        ])

    monkeypatch.setattr(quotes, "fetch_minute5", fake_fetch)
    monkeypatch.setattr(quotes, "realtime_quotes",
                        lambda codes, timeout=5.0: {})
    out = intraday.run_intraday(data_dir=str(tmp_path), push=False,
                                now=dt.datetime(2026, 9, 3, 10, 0))
    adds = [s for s in out["signals"] if s["code"] == "000001"
            and s["stype"] == "加仓"]
    assert adds, f"恢复态 + 斜率向上应产生试仓升级加仓: {out['signals']}"
    assert adds[0]["suggest_amount"] and adds[0]["suggest_amount"] > 0
    # 试仓升级预算 = base_max(50) - base_min(10) = 40% 权益
    assert adds[0]["suggest_amount"] == pytest.approx(3_000_000 * 0.4, rel=0.01)


# ---------------- 盘后：分钟线合并落库（write_minute5 为整文件覆盖） ----------------

def test_postclose_merges_minute5(tmp_path, monkeypatch):
    dates = _write_market(tmp_path)
    db.save_live_pool([{"code": "600000", "name": "股600000"}],
                      dates[-1], 0, [], None)
    old = pl.DataFrame([{"code": "600000", "date": f"{dates[-2]} 15:00",
                         "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                         "volume": 1.0, "amount": 10.0}])
    store.write_minute5("600000", old, str(tmp_path))

    def fake_fetch(code, day):
        return pl.DataFrame([{"code": code, "date": f"{day} 15:00",
                              "open": 11.0, "high": 11.0, "low": 11.0,
                              "close": 11.0, "volume": 2.0, "amount": 22.0}])

    monkeypatch.setattr(quotes, "fetch_minute5", fake_fetch)
    monkeypatch.setattr(quotes, "realtime_quotes",
                        lambda codes, timeout=5.0: {
                            "600000": {"name": "股600000", "price": 11.0,
                                       "prev_close": 11.0}})
    out = postclose.run_postclose(data_dir=str(tmp_path), push=False,
                                  now=dt.datetime(2026, 9, 3, 15, 40))
    assert out["saved"] == ["600000"]
    m = store.read_minute5("600000", data_dir=str(tmp_path))
    assert m.height == 2, "盘后落库必须合并历史而非覆盖"
    assert set(m["date"].to_list()) == {f"{dates[-2]} 15:00", f"{dates[-1]} 15:00"}


# ---------------- M3：影子账户 FIFO 与滑点统计 ----------------

def test_shadow_and_slippage():
    s1 = db.add_live_signal("premarket", "开仓", "600000", "股600000",
                            "加速启动", 10_000.0, 10.0)   # 影子：1000股@10
    s2 = db.add_live_signal("premarket", "清仓", "600000", "股600000",
                            "衰退清仓", None, 12.0)       # 影子：全卖@12
    db.add_live_fill(s1, "600000", "buy", 10.5, 1000, fee=0.0)   # 实际买 10.5
    db.add_live_fill(s2, "600000", "sell", 11.0, 1000, fee=0.0)  # 实际卖 11.0
    db.set_live_signal_status(s1, "已成交")  # API 回填链路会置状态，此处直调 db 需手动
    # s2 保持"待执行"：信号已发但未执行（影子账户仍按参考价计，执行率 50%）

    st = reports.shadow_stats()
    assert st["n_signals"] == 2 and st["n_filled"] == 1
    assert st["fill_rate"] == pytest.approx(0.5)
    assert st["shadow_pnl"] == pytest.approx(2000.0), "影子账户按参考价 10买12卖"
    assert st["actual_pnl"] == pytest.approx(500.0), "实际 FIFO：1000×(11−10.5)"
    assert st["gap_pnl"] == pytest.approx(-1500.0), "差额=滑点+执行偏差代价"

    slip = reports.slippage_stats()
    assert slip["summary"]["n"] == 2
    by_side = {r["side"]: r["slip_pct"] for r in slip["rows"]}
    assert by_side["buy"] == pytest.approx(5.0), "买 10.5 vs 参考 10 → 5% 滑点成本"
    assert by_side["sell"] == pytest.approx(8.3333), "卖 11.0 vs 参考 12 → 少卖 8.33%"
