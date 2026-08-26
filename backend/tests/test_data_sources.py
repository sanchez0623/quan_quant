# -*- coding: utf-8 -*-
"""代码归一化测试：统一纯 6 位数字格式（stock_basic/数据源/回测 universe 全链路）"""
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import sources, store, updater  # noqa: E402


# ---------------- _norm_code ----------------

@pytest.mark.parametrize("raw,expected", [
    ("600021", "600021"),       # 纯数字（标准格式）
    ("sh.600021", "600021"),    # baostock 带前缀
    ("sz.000001", "000001"),
    ("sh600021", "600021"),     # 无点前缀
    (" 600021 ", "600021"),     # 空白
])
def test_norm_code(raw, expected):
    assert sources._norm_code(raw) == expected


def test_bs_code_from_plain():
    assert sources._bs_code("600021") == "sh.600021"
    assert sources._bs_code("000001") == "sz.000001"
    assert sources._bs_code("830799") is None      # 北交所不支持


# ---------------- stock_basic 读写归一化 ----------------

def test_stock_basic_roundtrip_normalizes(tmp_path):
    df = pl.DataFrame({"code": ["sh.600021", "sz.000001"], "name": ["上海电力", "平安银行"],
                       "st": [False, False], "list_date": [None, None]})
    store.write_stock_basic(df, str(tmp_path))
    out = store.read_stock_basic(str(tmp_path))
    assert out["code"].to_list() == ["600021", "000001"]   # 写入即归一化


def test_stock_basic_read_legacy_prefixed_file(tmp_path):
    """历史文件（sh. 前缀）读取时归一化，无需重写文件"""
    legacy = pl.DataFrame({"code": ["sh.600021", "sh.600000"], "name": ["上海电力", "浦发银行"],
                           "st": [False, False], "list_date": [None, None]})
    legacy.write_parquet(tmp_path / "stock_basic.parquet")
    out = store.read_stock_basic(str(tmp_path))
    assert out["code"].to_list() == ["600021", "600000"]


# ---------------- updater._norm_codes ----------------

def test_bs_code_star_market_excluded():
    """科创板(688/689) 被 baostock 排除"""
    assert sources._bs_code("688146") is None
    assert sources._bs_code("689009") is None
    assert sources._bs_code("sh.688146") is None
    # 主板 600/601/603/605 仍支持
    assert sources._bs_code("600000") == "sh.600000"
    assert sources._bs_code("601318") == "sh.601318"
    assert sources._bs_code("603000") == "sh.603000"


def test_updater_norm_codes():
    assert updater._norm_codes(["sh.600021", "600000", "sh600000 ", ""]) == ["600021", "600000"]
    assert updater._norm_codes([]) is None
    assert updater._norm_codes(None) is None


# ---------------- 回测配置 universe 归一化 ----------------

def test_validate_backtest_normalizes_universe():
    from fastapi import HTTPException
    from app.api.backtests import validate_backtest_config
    cfg = {"strategy_id": "ma_cross", "universe": ["sh.600021", "600000", "sh600000"],
           "params": {"fast": 5, "slow": 20},
           "start_date": "2026-01-01", "end_date": "2026-06-30", "period": "daily"}
    out = validate_backtest_config(dict(cfg))
    assert out["universe"] == ["600021", "600000"]   # 去前缀 + 去重
    # 空列表 -> 400
    with pytest.raises(HTTPException):
        validate_backtest_config({**cfg, "universe": []})


# ---------------- 合成演示数据归一化 ----------------

def test_demo_data_normalizes_codes(tmp_path):
    from app.data import synthetic
    stats = synthetic.generate_demo_data(stocks=["sh.600021"], days=40,
                                         data_dir=str(tmp_path), seed=1)
    assert stats["stocks"] == 1
    daily = store.read_daily(None, str(tmp_path))
    assert daily["code"].unique().to_list() == ["600021"]
    assert (tmp_path / "minute5" / "600021.parquet").exists()   # 文件名纯数字，回测可命中
    basic = store.read_stock_basic(str(tmp_path))
    assert basic["code"].to_list() == ["600021"]
