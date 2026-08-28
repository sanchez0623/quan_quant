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
    ("600313.SH", "600313"),    # 乐咕成分股格式：代码在前、后缀在后
    ("000713.SZ", "000713"),
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


# ---------------- BaostockSource 登录缓存 / 会话失效自动重连 ----------------

class _FakeRs:
    def __init__(self, error_code, rows=(), fields=()):
        self.error_code = error_code
        self._rows = list(rows)
        self._fields = list(fields)

    @property
    def fields(self):
        return self._fields

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0) if self._rows else []


class _FakeBs:
    """假 baostock：统计登录/登出次数，可模拟查询级会话失效"""
    def __init__(self):
        self.login_calls = 0
        self.logout_calls = 0
        self.logged_in = False
        self.fail_next = False   # 下次查询返回错误码（模拟服务端断开会话）

    def login(self):
        self.login_calls += 1
        self.logged_in = True
        return _FakeRs("0")

    def logout(self):
        self.logout_calls += 1
        self.logged_in = False

    def query_history_k_data_plus(self, *a, **k):
        if self.fail_next:
            self.fail_next = False
            return _FakeRs("1011")   # 查询失败（会话失效）
        if not self.logged_in:
            return _FakeRs("1001")   # 未登录
        return _FakeRs("0", rows=[["2020-01-02", "1", "2", "3", "4", "5", "6"]],
                       fields=["date", "open", "high", "low", "close",
                               "volume", "amount"])


def test_baostock_login_cached_and_reconnect():
    src = sources.BaostockSource()
    fake = _FakeBs()
    src._bs = fake
    src._ok = True
    sources.BaostockSource._bs_logged_in = False  # 清类级登录态，避免测试间污染

    # 连续两次查询：登录态复用，只 login 一次、不登出
    d1 = src.get_daily("600000", "2020-01-01", "2020-01-10")
    d2 = src.get_daily("600000", "2020-01-02", "2020-01-05")
    assert d1 is not None and d2 is not None
    assert fake.login_calls == 1, "登录态应被缓存，第二次查询不应重新登录"
    assert fake.logout_calls == 0, "缓存登录态下不应每次查询后登出"

    # 会话失效（查询错误码）：自动登出并重登重试一次
    fake.fail_next = True
    d3 = src.get_daily("600000", "2020-01-01", "2020-01-10")
    assert d3 is not None, "会话失效后应自动重连重试"
    assert fake.login_calls == 2
    assert fake.logout_calls == 1

    # health_check 复用同一登录态，不额外登录
    assert src.health_check() is True
    assert fake.login_calls == 2, "health_check 不应触发额外登录"
