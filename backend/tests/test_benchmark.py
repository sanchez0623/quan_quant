# -*- coding: utf-8 -*-
"""基准对比（BENCHMARK）测试：对齐/前值填充/超额指标/数据缺失降级/校验"""
import polars as pl
import pytest
from fastapi import HTTPException

from app.api.backtests import validate_backtest_config
from app.data import store
from app.engine.runner import _attach_benchmark


def _mk_report(benchmark="000905"):
    return {
        "config": {"benchmark": benchmark, "initial_capital": 1_000_000.0},
        "metrics": {"total_return": -0.01},
        # 3 个交易日；指数数据故意缺 2025-01-03（验证前值填充）
        "equity_curve": [
            {"date": "2025-01-02", "equity": 1_000_000.0, "drawdown": 0.0},
            {"date": "2025-01-03", "equity": 1_010_000.0, "drawdown": 0.0},
            {"date": "2025-01-06", "equity": 990_000.0, "drawdown": 0.01},
        ],
    }


def _write_idx(dates_closes, key="000905", tmp=None):
    idx = pl.DataFrame({
        "index_key": [key] * len(dates_closes),
        "date": [d for d, _ in dates_closes],
        "close": [c for _, c in dates_closes],
        "open": [c for _, c in dates_closes], "high": [c for _, c in dates_closes],
        "low": [c for _, c in dates_closes],
        "volume": [0.0] * len(dates_closes), "amount": [0.0] * len(dates_closes),
    })
    store.write_index_daily(idx, str(tmp))


def test_benchmark_align_fill_and_metrics(tmp_path):
    _write_idx([("2025-01-02", 4000.0), ("2025-01-06", 3980.0)], tmp=tmp_path)
    rep = _mk_report()
    _attach_benchmark(rep, str(tmp_path))
    b = rep["benchmark"]
    assert b["index_key"] == "000905" and b["name"] == "中证500"
    # 前值填充：2025-01-03 无指数数据 -> 用 01-02 的 4000
    assert b["curve"][0]["equity"] == 1_000_000.0, "首日应归一化到初始资金"
    assert b["curve"][1]["close"] == 4000.0
    assert b["curve"][2]["close"] == 3980.0
    assert b["return"] == round(3980.0 / 4000.0 - 1, 6)
    m = rep["metrics"]
    assert m["benchmark_return"] == b["return"]
    assert m["excess_return"] == round(m["total_return"] - b["return"], 6)


def test_benchmark_degrades_without_data(tmp_path):
    """指数未拉取：静默降级——不写 benchmark、不加指标，回测本身不受影响"""
    rep = _mk_report()
    _attach_benchmark(rep, str(tmp_path))
    assert "benchmark" not in rep
    assert "benchmark_return" not in rep["metrics"]
    assert "excess_return" not in rep["metrics"]


def test_benchmark_degrades_when_range_uncovered(tmp_path):
    """指数区间不覆盖回测窗口：同样静默降级"""
    _write_idx([("2024-01-02", 4000.0), ("2024-01-03", 4010.0)], tmp=tmp_path)
    rep = _mk_report()
    _attach_benchmark(rep, str(tmp_path))
    assert "benchmark" not in rep


def test_validate_benchmark_field():
    def cfg(**over):
        c = {"name": "v", "strategy_id": "ma_cross", "params": {},
             "universe": ["600000"], "start_date": "2025-01-01",
             "end_date": "2025-06-01", "period": "daily"}
        c.update(over)
        return c

    out = validate_backtest_config(cfg(benchmark="000300"))
    assert out["benchmark"] == "000300"
    with pytest.raises(HTTPException):
        validate_backtest_config(cfg(benchmark="nasdaq"))
