# -*- coding: utf-8 -*-
"""UNIVERSE_PICKER 测试：板块推导 / 指数成分与申万行业存储 /
条件选股过滤与可复现抽样 / 更新失败安全（kept_old）"""
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import sources, store, updater  # noqa: E402
from app.data import industry  # noqa: E402
from app.api import stocks as stocks_api  # noqa: E402


# ---------------- 板块推导 ----------------

@pytest.mark.parametrize("code,expected", [
    ("600000", "main"),
    ("000001", "main"),
    ("300139", "chinext"),
    ("688146", "star"),
    ("689009", "star"),
    ("830799", "bse"),
    ("920001", "bse"),
    ("sh.600000", "main"),
    ("sz.300139", "chinext"),
    ("123456", None),
    ("", None),
])
def test_derive_board(code, expected):
    assert sources.derive_board(code) == expected


# ---------------- 指数成分 / 申万行业存储（读时归一化） ----------------

def test_index_constituents_store_roundtrip(tmp_path):
    df = pl.DataFrame({
        "index_key": ["hs300", "hs300"],
        "code": ["sh.600000", "000001"],
        "name": ["浦发银行", "平安银行"],
        "update_date": ["2026-08-24", "2026-08-24"],
        "snapshot_date": ["2026-08-27", "2026-08-27"],
    })
    store.write_index_constituents(df, str(tmp_path))
    out = store.read_index_constituents(str(tmp_path))
    assert out["code"].to_list() == ["600000", "000001"]


def test_stock_industry_store_roundtrip(tmp_path):
    df = pl.DataFrame({
        "code": ["sz.000001", "600000"],
        "sw_l1": ["银行", "银行"],
        "sw_l2": ["股份制银行", "国有大型银行"],
        "sw_l3": ["银行", "银行"],
        "sw_code": ["801780.SI", "801780.SI"],
        "snapshot_date": ["2026-08-27", "2026-08-27"],
    })
    store.write_stock_industry(df, str(tmp_path))
    out = store.read_stock_industry(str(tmp_path))
    assert out["code"].to_list() == ["000001", "600000"]


# ---------------- 条件选股（过滤 + 可复现抽样） ----------------

@pytest.fixture()
def pick_env(tmp_path, monkeypatch):
    """临时 DATA_DIR + 合成 stock_basic / index_constituents / stock_industry"""
    monkeypatch.setattr(store.config, "DATA_DIR", tmp_path)
    store.write_stock_basic(pl.DataFrame({
        "code": ["600000", "000001", "300139", "688146", "830799", "600036"],
        "name": ["浦发银行", "平安银行", "晓程科技", "某科创", "某北交所", "招商银行"],
        "st": [False, False, False, False, True, False],
        "list_date": ["2010-01-01"] * 6,
    }), str(tmp_path))
    store.write_index_constituents(pl.DataFrame({
        "index_key": ["hs300"] * 4 + ["csi800"] * 2,
        "code": ["600000", "000001", "300139", "600036", "600000", "000001"],
        "name": ["浦发银行", "平安银行", "晓程科技", "招商银行", "浦发银行", "平安银行"],
        "update_date": ["2026-08-24"] * 6,
        "snapshot_date": ["2026-08-27"] * 6,
    }), str(tmp_path))
    store.write_stock_industry(pl.DataFrame({
        "code": ["600000", "000001", "300139", "688146", "600036"],
        "sw_l1": ["银行", "银行", "电力设备", "电子", "银行"],
        "sw_l2": ["国有大型银行", "股份制银行", "电网设备", "半导体", "股份制银行"],
        "sw_l3": ["银行", "银行", "输变电设备", "模拟芯片", "银行"],
        "sw_code": ["801780.SI", "801780.SI", "801731.SI", "801081.SI", "801780.SI"],
        "snapshot_date": ["2026-08-27"] * 5,
    }), str(tmp_path))
    return tmp_path


def _pick(req: dict):
    return stocks_api.pick_stocks(stocks_api.PickRequest(**req), _user="test")


def test_pick_index_filter_and_st(pick_env):
    # 沪深300 ∧ 剔除ST -> 4 只（600000/000001/300139/600036）
    res = _pick({"filters": {"index": "hs300", "exclude_st": True}})
    assert res["total_matched"] == 4
    assert set(res["codes"]) == {"600000", "000001", "300139", "600036"}
    assert res["meta"]["filters"]["index"] == "hs300"
    assert res["name_map"]["600000"] == "浦发银行"


def test_pick_industry_and_board(pick_env):
    # 行业(一级)=银行 -> 3 只
    res = _pick({"filters": {"industry_l1": ["银行"]}})
    assert set(res["codes"]) == {"600000", "000001", "600036"}
    # 行业(二级)=股份制银行 -> 2 只
    res = _pick({"filters": {"industry_l2": ["股份制银行"]}})
    assert set(res["codes"]) == {"000001", "600036"}
    # 行业(三级)=输变电设备 -> 1 只
    res = _pick({"filters": {"industry_l3": ["输变电设备"]}})
    assert set(res["codes"]) == {"300139"}
    # 板块=北交所 剔除ST -> 0；不剔除 -> 1
    res = _pick({"filters": {"boards": ["bse"], "exclude_st": True}})
    assert res["total_matched"] == 0
    res = _pick({"filters": {"boards": ["bse"], "exclude_st": False}})
    assert res["codes"] == ["830799"]


def test_pick_random_reproducible(pick_env):
    req = {"filters": {"index": "hs300", "exclude_st": True},
           "random": {"n": 2, "seed": 42}}
    a, b = _pick(req), _pick(req)
    assert a["codes"] == b["codes"], "同 seed 必须同池子"
    assert len(a["codes"]) == 2
    assert a["seed_used"] == 42
    assert a["total_matched"] == 4 and a["total_picked"] == 2

    # seed 缺省 -> 后端生成并回传
    c = _pick({"filters": {"index": "hs300"}, "random": {"n": 2}})
    assert c["seed_used"] is not None and len(c["codes"]) == 2

    # n >= 命中数 -> 全取 + truncated 提示
    d = _pick({"filters": {"index": "hs300"}, "random": {"n": 99, "seed": 1}})
    assert d["truncated"] is True and d["total_picked"] == d["total_matched"] == 4


def test_pick_options_tree_and_counts(pick_env):
    res = stocks_api.pick_options(_user="test")
    assert res["index_snapshot"] == "2026-08-27"
    assert res["industry_snapshot"] == "2026-08-27"
    idx = {i["key"]: i["count"] for i in res["indices"]}
    assert idx["hs300"] == 4 and idx["csi800"] == 2
    boards = {b["key"]: b["count"] for b in res["boards"]}
    assert boards["main"] == 3 and boards["chinext"] == 1 and boards["star"] == 1
    # 行业树：银行(3) -> 股份制银行(2) -> 银行(2)
    bank = next(n for n in res["industry_tree"] if n["value"] == "银行")
    assert bank["count"] == 3
    l2 = next(n for n in bank["children"] if n["value"] == "股份制银行")
    assert l2["count"] == 2
    assert next(n for n in l2["children"] if n["value"] == "银行")["count"] == 2


def test_pick_missing_data_raises(pick_env, monkeypatch):
    # 删掉行业表 -> 使用行业过滤时报 400
    from fastapi import HTTPException
    monkeypatch.setattr(store, "read_stock_industry", lambda data_dir=None: None)
    with pytest.raises(HTTPException):
        _pick({"filters": {"industry_l1": ["银行"]}})


# ---------------- 更新失败安全（kept_old） ----------------

def test_lixinger_to_rows_three_level():
    """理杏仁返回 -> 三级行业行（只取三级成分，前缀推导 l1/l2）"""
    ind_list = [
        {"stockCode": "110000", "name": "农林牧渔", "level": "one"},
        {"stockCode": "110100", "name": "种植业", "level": "two"},
        {"stockCode": "110101", "name": "种子", "level": "three"},
        {"stockCode": "110101", "name": "种子", "level": "three"},
        {"stockCode": "120000", "name": "银行", "level": "one"},
    ]
    cons_data = [
        # 三级行业：取成分并解析
        {"stockCode": "110101", "constituents": [
            {"stockCode": "600313", "stockName": {"cmn_hans_cn": "农发种业"}},
            {"stockCode": "000713", "stockName": {"cmn_hans_cn": "国投丰乐"}},
        ]},
        # 一级行业：应被跳过（避免超集重复）
        {"stockCode": "110000", "constituents": [
            {"stockCode": "600313", "stockName": {"cmn_hans_cn": "农发种业"}},
        ]},
        # 未知代码行业：跳过
        {"stockCode": "999999", "constituents": [
            {"stockCode": "000001", "stockName": {"cmn_hans_cn": "平安银行"}},
        ]},
    ]
    rows = industry._lixinger_to_rows(ind_list, cons_data)
    assert len(rows) == 2
    r = next(x for x in rows if x["code"] == "600313")
    assert r["sw_l1"] == "农林牧渔" and r["sw_l2"] == "种植业"
    assert r["sw_l3"] == "种子" and r["sw_code"] == "110101"
    assert {x["code"] for x in rows} == {"600313", "000713"}


def test_update_industry_prefers_lixinger(tmp_path, monkeypatch):
    """LIXINGER_API_KEY 存在时优先走理杏仁路径，并标注 source=lixinger"""
    monkeypatch.setenv("LIXINGER_API_KEY", "test-key")
    monkeypatch.setattr(updater, "_fetch_all_index_constituents", lambda: None)
    monkeypatch.setattr(updater, "sync_index_history",
                        lambda *a, **k: {"skipped": "test"})  # 历史快照单独测，不打真网
    monkeypatch.setattr(industry, "fetch_sw_industry_lixinger",
                        lambda api_key, progress_cb=None: pl.DataFrame({
                            "code": ["600000"], "sw_l1": ["银行"], "sw_l2": ["银行"],
                            "sw_l3": ["银行"], "sw_code": ["110000"],
                            "snapshot_date": ["2026-08-28"],
                        }))
    stats = updater.update_industry(data_dir=str(tmp_path))
    assert stats["industry_rows"] == 1
    assert stats["industry_source"] == "lixinger"
    assert store.read_stock_industry(str(tmp_path))["code"].to_list() == ["600000"]


def test_update_industry_keeps_old_on_failure(tmp_path, monkeypatch):
    """指数/行业拉取失败但本地有旧数据 -> 保留旧数据并标注 kept_old"""
    monkeypatch.delenv("LIXINGER_API_KEY", raising=False)  # 强制走乐咕路径（被测逻辑）
    store.write_index_constituents(pl.DataFrame({
        "index_key": ["hs300"], "code": ["600000"], "name": ["浦发银行"],
        "update_date": ["2026-01-01"], "snapshot_date": ["2026-01-01"],
    }), str(tmp_path))
    store.write_stock_industry(pl.DataFrame({
        "code": ["600000"], "sw_l1": ["银行"], "sw_l2": ["银行"],
        "sw_l3": ["银行"], "sw_code": ["801780.SI"], "snapshot_date": ["2026-01-01"],
    }), str(tmp_path))
    monkeypatch.setattr(updater, "_fetch_all_index_constituents", lambda: None)
    monkeypatch.setattr(updater, "sync_index_history",
                        lambda *a, **k: {"skipped": "test"})
    monkeypatch.setattr(industry, "crawl_sw_industry",
                        lambda progress_cb=None: (_ for _ in ()).throw(RuntimeError("网络不可用")))
    stats = updater.update_industry(data_dir=str(tmp_path))
    assert stats["kept_old"] == {"index": True, "industry": True}
    # 旧数据仍在
    assert store.read_index_constituents(str(tmp_path))["code"].to_list() == ["600000"]
    assert store.read_stock_industry(str(tmp_path))["code"].to_list() == ["600000"]


def test_update_industry_writes_new(tmp_path, monkeypatch):
    monkeypatch.delenv("LIXINGER_API_KEY", raising=False)  # 强制走乐咕路径（被测逻辑）
    monkeypatch.setattr(updater, "_fetch_all_index_constituents", lambda: [
        {"index_key": "hs300", "code": "600000", "name": "浦发银行",
         "update_date": "2026-08-24"},
    ])
    monkeypatch.setattr(updater, "sync_index_history",
                        lambda *a, **k: {"skipped": "test"})
    monkeypatch.setattr(industry, "crawl_sw_industry",
                        lambda progress_cb=None: pl.DataFrame({
                            "code": ["600000"], "sw_l1": ["银行"], "sw_l2": ["银行"],
                            "sw_l3": ["银行"], "sw_code": ["801780.SI"],
                            "snapshot_date": ["2026-08-27"],
                        }))
    stats = updater.update_industry(data_dir=str(tmp_path))
    assert stats["kept_old"] == {}
    assert stats["index_rows"] == 1 and stats["industry_rows"] == 1


# ---------------- 股票列表刷新（scope=stock_basic：ST / 退市标记） ----------------

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
    """假 baostock：query_all_stock 返回 4 只在市证券（含 1 只 ST、1 只指数应被跳过）"""
    def __init__(self):
        self.logged_in = False
        self.rows = [
            ["sh.000001", "1", "上证指数"],       # 指数：derive_board 无法识别 -> 跳过
            ["sh.600000", "1", "浦发银行"],       # 在市
            ["sz.000001", "1", "平安银行"],       # 在市
            ["sz.300139", "1", "*ST晓程"],        # 在市 + ST（名称含 ST）
            ["sh.600036", "0", "招商银行"],       # tradeStatus=0（停牌）仍算在市集合
        ]

    def login(self):
        self.logged_in = True
        return _FakeRs("0")

    def logout(self):
        self.logged_in = False

    def query_all_stock(self, day=""):
        return _FakeRs("0", rows=[list(r) for r in self.rows],
                       fields=["code", "tradeStatus", "code_name"])


def test_update_stock_basic_marks_st_and_delisted(tmp_path, monkeypatch):
    """scope=stock_basic：用 query_all_stock 重写 st、标记 delisted"""
    monkeypatch.setattr(store.config, "DATA_DIR", tmp_path)
    store.write_stock_basic(pl.DataFrame({
        "code": ["600000", "000001", "300139", "600036", "600999"],
        "name": ["浦发银行", "平安银行", "晓程科技", "招商银行", "退市老股"],
        "st": [False, False, False, False, False],
        "list_date": ["2010-01-01"] * 5,
    }), str(tmp_path))

    src = sources.BaostockSource()
    src._bs = _FakeBs()
    src._ok = True
    sources.BaostockSource._bs_logged_in = False
    monkeypatch.setattr(updater.sources, "SOURCES", [src])
    monkeypatch.setattr(updater, "time", type("T", (), {"strftime": staticmethod(lambda *a: "2026-08-28")})())

    stats = updater.update_stock_basic(data_dir=str(tmp_path))
    assert stats["total"] == 5
    assert stats["st_count"] == 1          # 300139（*ST晓程）
    assert stats["delisted_count"] == 1    # 600999 不在在市集合

    out = store.read_stock_basic(str(tmp_path))
    m = {r["code"]: r for r in out.to_dicts()}
    assert m["300139"]["st"] is True
    assert m["600999"]["delisted"] is True
    assert m["600000"]["delisted"] is False
    assert m["600000"]["st"] is False
    assert "delisted" in out.columns


def test_read_stock_basic_legacy_without_delisted(tmp_path, monkeypatch):
    """旧 schema（无 delisted 列）读取时补齐 False"""
    monkeypatch.setattr(store.config, "DATA_DIR", tmp_path)
    store.write_stock_basic(pl.DataFrame({
        "code": ["600000"], "name": ["浦发银行"], "st": [False],
        "list_date": [None],
    }), str(tmp_path))
    out = store.read_stock_basic(str(tmp_path))
    assert "delisted" in out.columns
    assert out["delisted"].to_list() == [False]


def test_search_bycodes_pick_exclude_st_and_delisted(pick_env):
    """搜索/批量/条件选股均排除 ST 与退市股"""
    # 给 pick_env 补充退市股：600036 之外再加一只在市 + 一只退市
    store.write_stock_basic(pl.DataFrame({
        "code": ["600000", "000001", "300139", "688146", "830799", "600036", "600999"],
        "name": ["浦发银行", "平安银行", "晓程科技", "某科创", "某北交所", "招商银行", "退市老股"],
        "st": [False, False, False, False, True, False, False],
        "delisted": [False, False, False, False, False, False, True],
        "list_date": ["2010-01-01"] * 7,
    }), str(pick_env))

    # 搜索：排除 ST 与退市
    rows = stocks_api.search_stocks(keyword="", limit=100, _user="test")
    codes = {r["code"] for r in rows}
    assert "830799" not in codes, "ST 股不应出现在搜索结果"
    assert "600999" not in codes, "退市股不应出现在搜索结果"
    assert "600000" in codes

    # 批量 by-codes：退市/ST 不应返回
    rows = stocks_api.stocks_by_codes(codes="600000,830799,600999", _user="test")
    assert [r["code"] for r in rows] == ["600000"]

    # 条件选股：无条件排除退市（ST 按开关）
    res = _pick({"filters": {"boards": ["bse"], "exclude_st": False}})
    assert "600999" not in res["codes"], "退市股无条件排除"
    res = _pick({"filters": {"boards": ["bse"], "exclude_st": False}})
    assert res["codes"] == ["830799"], "ST 开关关时 ST 可入选、退市不可"
