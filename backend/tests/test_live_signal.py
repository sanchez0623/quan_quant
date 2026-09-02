# -*- coding: utf-8 -*-
"""实盘信号机测试（LIVE_SIGNAL_SYSTEM M1）：
DATA_GUARD 完整性守卫 / 盘前流程 / 成交回填联动 / 虚拟持仓对账。
注意：run_premarket 测试一律 push=False（.env 有真实 webhook，避免测试期发消息）。
"""
import numpy as np
import polars as pl
import pytest

from app import db
from app.data import store, synthetic
from app.engine.runner import run_backtest  # noqa: F401（确保引擎可导入）
from app.live import premarket

N_DAYS = 330


@pytest.fixture(autouse=True)
def _sig_env():
    """meta.db 建信号机表（幂等）+ 测试后清空 sig_* 行（防污染真实库）"""
    db.init_db()
    yield
    with db.conn() as c:
        for t in ("sig_signal_log", "sig_fills", "sig_position",
                  "sig_pool", "sig_config", "sig_t_debt", "sig_withdraw"):
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
    dates = synthetic.trade_dates(N_DAYS)
    store.write_daily(_rows(["600000", "600036", "000001", "000002"], dates), str(tmp_path))
    with pytest.raises(ValueError, match="DATA_GUARD"):
        store.write_daily(_rows(["600000", "600036"], dates), str(tmp_path))


def test_data_guard_start_push_rejected(tmp_path):
    dates = synthetic.trade_dates(N_DAYS)
    store.write_daily(_rows(["600000"], dates), str(tmp_path))
    # 同一只票但只写后 200 天：票数不变、日期起点推迟 -> 守卫拦截（老历史丢失特征）
    with pytest.raises(ValueError, match="DATA_GUARD"):
        store.write_daily(_rows(["600000"], dates, start_i=130), str(tmp_path))


def test_data_guard_normal_merge_ok(tmp_path):
    """合规路径：读旧表 -> concat -> 去重 -> 写回，守卫不误伤"""
    dates = synthetic.trade_dates(N_DAYS)
    old = _rows(["600000", "600036"], dates)
    store.write_daily(old, str(tmp_path))
    new = _rows(["000001", "000002"], dates)
    merged = pl.concat([store.read_daily(None, str(tmp_path)), new])
    store.write_daily(merged, str(tmp_path))
    assert store.read_daily(None, str(tmp_path))["code"].n_unique() == 4


# ---------------- 盘前流程（M1） ----------------

def _write_market(tmp_path):
    dates = synthetic.trade_dates(N_DAYS)
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
    assert pos["600000"]["cost_price"] == 10.5
    assert db.list_live_signals(limit=10)[0]["status"] == "已成交" \
        if db.list_live_signals(limit=10)[0]["id"] == sid else True
    # 卖出清仓 -> 持仓删除
    add_fill(FillBody(signal_id=None, code="600000", side="sell",
                      fill_price=11.0, fill_volume=10000))
    assert not any(p["code"] == "600000" for p in db.list_live_positions())


def test_signal_status_validation():
    from app.api.live import ALLOWED_SIGNAL_STATUS, SignalStatusBody
    assert "已成交" in ALLOWED_SIGNAL_STATUS
    body = SignalStatusBody(status="已成交")
    assert body.status == "已成交"
