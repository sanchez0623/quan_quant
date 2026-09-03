# -*- coding: utf-8 -*-
"""实盘信号机测试（LIVE_SIGNAL_SYSTEM M1）：
DATA_GUARD 完整性守卫 / 盘前流程 / 成交回填联动 / 虚拟持仓对账。
注意：run_premarket 测试一律 push=False（.env 有真实 webhook，避免测试期发消息）。
"""
import datetime as dt

import numpy as np
import polars as pl
import pytest

from app import db
from app.data import store, synthetic
from app.engine.runner import run_backtest  # noqa: F401（确保引擎可导入）
from app.live import premarket

N_DAYS = 330
_TODAY = dt.date(2026, 9, 3)   # 测试日锚：合成日历尾部恒定（防真实日期漂移，
                               # 与 test_live_intraday.TODAY 对齐）


@pytest.fixture(autouse=True)
def _sig_env():
    """meta.db 建信号机表（幂等）+ 测试后清空 sig_* 行（防污染真实库）"""
    db.init_db()
    yield
    with db.conn() as c:
        for t in ("sig_signal_log", "sig_fills", "sig_position",
                  "sig_pool", "sig_config", "sig_t_debt", "sig_withdraw",
                  "sig_strategy_state", "sig_meta"):
            c.execute(f"DELETE FROM {t}")


# ---------------- DATA_GUARD 完整性守卫 ----------------

def _rows(codes, dates, start_i=0):
    rows = []
    for ci, code in enumerate(codes):
        prev = 10.0
        for d in dates[start_i:]:
            c = prev * 1.001
            rows.append({"code": code, "date": d,
                         "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                         "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                         "volume": 1_000_000, "amount": 0.0})
            prev = c
    return pl.DataFrame(rows)


def test_data_guard_shrink_rejected(tmp_path):
    dates = synthetic.trade_dates(N_DAYS, end_date=_TODAY)
    store.write_daily(_rows(["600000", "600036", "000001", "000002"], dates), str(tmp_path))
    with pytest.raises(ValueError, match="DATA_GUARD"):
        store.write_daily(_rows(["600000", "600036"], dates), str(tmp_path))


def test_data_guard_start_push_rejected(tmp_path):
    dates = synthetic.trade_dates(N_DAYS, end_date=_TODAY)
    store.write_daily(_rows(["600000"], dates), str(tmp_path))
    # 同一只票但只写后 200 天：票数不变、日期起点推迟 -> 守卫拦截（老历史丢失特征）
    with pytest.raises(ValueError, match="DATA_GUARD"):
        store.write_daily(_rows(["600000"], dates, start_i=130), str(tmp_path))


def test_data_guard_normal_merge_ok(tmp_path):
    """合规路径：读旧表 -> concat -> 去重 -> 写回，守卫不误伤"""
    dates = synthetic.trade_dates(N_DAYS, end_date=_TODAY)
    old = _rows(["600000", "600036"], dates)
    store.write_daily(old, str(tmp_path))
    new = _rows(["000001", "000002"], dates)
    merged = pl.concat([store.read_daily(None, str(tmp_path)), new])
    store.write_daily(merged, str(tmp_path))
    assert store.read_daily(None, str(tmp_path))["code"].n_unique() == 4


# ---------------- 盘前流程（M1） ----------------

def _write_market(tmp_path):
    dates = synthetic.trade_dates(N_DAYS, end_date=_TODAY)
    plans = {
        "600000": [(0, 210, 0.003), (210, N_DAYS, -0.006)],
        "600036": [(0, 210, 0.003), (210, N_DAYS, -0.006)],
        "000001": [(210, N_DAYS, 0.003)],
        "000002": [(210, N_DAYS, 0.003)],
    }
    rows = []
    for ci, (code, segs) in enumerate(plans.items()):
        rng = np.random.default_rng(42 + ci)
        prev = 10.0
        for di, d in enumerate(dates):
            ret = next((r for (s, e, r) in segs if s <= di < e), 0.0)
            c = prev * (1 + ret + float(rng.normal(0, 0.0005)))
            rows.append({"code": code, "date": d,
                         "open": round(prev, 4), "high": round(max(prev, c) * 1.002, 4),
                         "low": round(min(prev, c) * 0.998, 4), "close": round(c, 4),
                         "volume": 1_000_000, "amount": round(c * 1_000_000, 2)})
            prev = c
    store.write_daily(pl.DataFrame(rows), str(tmp_path))
    store.write_stock_basic(pl.DataFrame({
        "code": list(plans), "name": [f"股{c}" for c in plans],
        "st": [False] * len(plans), "list_date": ["20000101"] * len(plans)}),
        str(tmp_path))
    return dates


def test_premarket_rebalance(tmp_path):
    """空仓首跑：动态重选产生开仓信号 + 池子落库 + 飞书未配置时 pushed=False"""
    dates = _write_market(tmp_path)
    db.save_live_config({"auto_idle_days": 5, "top_x": 2, "auto_index": [],
                         "auto_boards": [], "exit_need": 2})
    result = premarket.run_premarket(data_dir=str(tmp_path), push=False)
    assert result["rebalanced"] is True, "空仓首跑应触发动态重选"
    assert result["pushed"] is False, "未配置飞书时应静默降级"
    assert 1 <= len(result["signals"]) <= 2, \
        f"开仓信号应与过门槛票数(≤top_x)一致: {result['signals']}"
    for s in result["signals"]:
        assert s["ref_price"] is not None, \
            "参考价应为 T-1 收盘价（d_close），而非动量分"
        assert 0 < s["ref_price"] < 1000
        assert s["name"] != s["code"], "名称应来自 stock_basic 而非回退代码"
    assert result["gate_state"] == 0
    # 池子状态滚动
    pool = db.get_live_pool()
    assert len(pool["pool"]) == len(result["signals"]) and pool["as_of"] == result["as_of"]
    assert pool["idle_start"] is None, "重选后不再空仓"
    # 信号流水
    sigs = db.list_live_signals()
    assert any(s["stype"] == "开仓" for s in sigs)
    assert any(s["stype"] == "池子" for s in sigs)


def test_premarket_exit_warning(tmp_path):
    """持仓票 A 组走坏（死叉+跌破均线+动量转负）：盘前推清仓预警"""
    dates = _write_market(tmp_path)
    # 模拟：持仓 600000（A 组 210 日后走坏），当前日期在走坏之后
    db.upsert_live_position("600000", "股600000", 10000, 10.0,
                            open_day=dates[100])
    db.save_live_pool([{"code": "600000", "name": "股600000"}], dates[209],
                      0, [], None)
    db.save_live_config({"auto_idle_days": 5, "top_x": 2, "auto_index": [],
                         "auto_boards": [], "exit_need": 2})
    result = premarket.run_premarket(data_dir=str(tmp_path), push=False)
    warn_codes = [w["code"] for w in result["warns"]]
    assert "600000" in warn_codes, (
        f"走坏持仓应触发清仓预警，实际 warns={result['warns']} "
        f"messages={result['message']}")


# ---------------- 成交回填联动 ----------------

def test_fill_updates_position_and_signal(tmp_path):
    _write_market(tmp_path)
    sid = db.add_live_signal("premarket", "开仓", "600000", "股600000",
                             "动态重选入池", 150000.0, None)
    from app.api.live import add_fill, FillBody
    body = FillBody(signal_id=sid, code="600000", side="buy",
                    fill_price=10.5, fill_volume=10000, fee=5.0)
    add_fill(body)
    pos = {p["code"]: p for p in db.list_live_positions()}
    assert pos["600000"]["volume"] == 10000
    assert pos["600000"]["cost_price"] == pytest.approx(10.5005, abs=0.001), \
        "手填 fee 5 元也应摊入成本"
    # 回填联动状态机：买入建仓 -> opened/full 置位（策略大脑知道真实持仓）
    st = db.get_strategy_states()["600000"]["st"]
    assert st["opened"] == 1 and st["full"] == 1
    assert db.list_live_signals(limit=10)[0]["status"] == "已成交" \
        if db.list_live_signals(limit=10)[0]["id"] == sid else True
    # 卖出清仓 -> 持仓删除 + 状态机复位
    add_fill(FillBody(signal_id=None, code="600000", side="sell",
                      fill_price=11.0, fill_volume=10000))
    assert not any(p["code"] == "600000" for p in db.list_live_positions())
    st = db.get_strategy_states()["600000"]["st"]
    assert st["opened"] == 0 and st["exit_stage"] == 0


def test_fill_fee_auto_and_cost_dilution(tmp_path):
    """手续费：缺省按交易成本费率自动计算（Broker 同口径）；买入费用摊入
    持仓成本价（对齐券商摊薄口径）；手填 fee 覆盖自动值"""
    _write_market(tmp_path)
    sid = db.add_live_signal("premarket", "开仓", "600000", "股600000",
                             "动态重选入池", 150000.0, None)
    from app.api.live import add_fill, FillBody
    add_fill(FillBody(signal_id=sid, code="600000", side="buy",
                      fill_price=10.5, fill_volume=10000))
    # 自动计费：105000 元 -> 佣金 max(万0.5,5)=5.25 + 双边杂费 6.7305 = 11.98
    f_buy = [f for f in db.list_live_fills(limit=10) if f["side"] == "buy"][0]
    assert f_buy["fee"] == pytest.approx(11.98, abs=0.02)
    # 费用摊入成本：(105000 + 11.98) / 10000 = 10.5012
    pos = db.list_live_positions()[0]
    assert pos["cost_price"] == pytest.approx(10.5012, abs=0.0005)
    # 手填 fee 覆盖自动值
    add_fill(FillBody(signal_id=None, code="600000", side="buy",
                      fill_price=10.5, fill_volume=100, fee=3.0))
    f_manual = [f for f in db.list_live_fills(limit=10) if f["fee"] == 3.0]
    assert len(f_manual) == 1, "手填 fee 应原样入账"
    # 卖出自动计费含印花税：11.0×10100=111,100 -> 5.555+55.55+7.1215=68.23
    add_fill(FillBody(signal_id=None, code="600000", side="sell",
                      fill_price=11.0, fill_volume=10100))
    f_sell = [f for f in db.list_live_fills(limit=10) if f["side"] == "sell"][0]
    assert f_sell["fee"] == pytest.approx(68.23, abs=0.02)


def test_signal_status_validation():
    from app.api.live import ALLOWED_SIGNAL_STATUS, SignalStatusBody
    assert "已成交" in ALLOWED_SIGNAL_STATUS
    body = SignalStatusBody(status="已成交")
    assert body.status == "已成交"


# ---------------- 持仓现价快照（浮盈展示） ----------------

def test_position_price_snapshot(tmp_path, monkeypatch):
    """盘中轮询/盘后流程落库持仓现价；list_live_positions 带浮盈展示字段"""
    from app.live import intraday, postclose
    db.upsert_live_position("600000", "股600000", 10000, 10.0,
                            open_day="2026-09-01")
    # 盘中轮询：qt 报价 mock，分钟线空数据（只走落库路径，不进状态机）
    monkeypatch.setattr(intraday.quotes, "realtime_quotes",
                        lambda codes: {c: {"price": 10.5, "prev_close": 10.0,
                                           "name": "股600000"} for c in codes})
    monkeypatch.setattr(intraday.quotes, "fetch_minute5",
                        lambda code, day: None)
    intraday.run_intraday(data_dir=str(tmp_path), push=False)
    pos = {p["code"]: p for p in db.list_live_positions()}["600000"]
    assert pos["last_price"] == 10.5
    assert pos["last_ts"]
    # 盘后流程更新为收盘快照 10.8
    monkeypatch.setattr(postclose.quotes, "fetch_minute5",
                        lambda code, day: None)
    monkeypatch.setattr(postclose.quotes, "realtime_quotes",
                        lambda codes: {c: {"price": 10.8, "prev_close": 10.0}
                                       for c in codes})
    postclose.run_postclose(data_dir=str(tmp_path), push=False)
    pos = db.list_live_positions()[0]
    assert pos["last_price"] == 10.8
