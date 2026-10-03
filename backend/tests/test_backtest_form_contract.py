# -*- coding: utf-8 -*-
"""回测顶层字段契约测试（single source of truth 的门禁）。

覆盖三类历史事故：
  · 模型漏字段：pool_refill_min 未进 BacktestRequest -> pydantic 静默丢弃前端传值；
  · 登记表漏字段：auto_rank_key / nav_take_profit_pct 未登记 -> 模板落库缺失；
  · 前后端默认值不一致：前端 2 vs 引擎 0=关闭 -> 用户建的任务被静默配成已证伪组合。
"""
import re
from pathlib import Path

import pytest

from app.api.backtest_schema import TOP_FIELDS, UI_FIELDS, field_map, top_level_defaults
from app.api.backtests import BacktestRequest, backtest_form_meta, validate_backtest_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKTEST_LIST_TSX = _REPO_ROOT / "frontend" / "src" / "pages" / "BacktestList.tsx"


def _base_cfg(**over):
    cfg = {
        "name": "契约测试",
        "strategy_id": "momentum_t",
        "params": {},
        "start_date": "2024-01-02",
        "end_date": "2024-06-30",
        "universe": ["600000"],
    }
    cfg.update(over)
    if cfg.get("universe_auto"):
        # 动态选股要求 universe 留空（池子由预筛生成）
        cfg["universe"] = []
    return cfg


# ---------------------------------------------------------------- 模型 <-> schema


def test_request_model_fields_match_schema_exactly():
    """BacktestRequest 由 schema 生成：两边的键集合必须完全一致。"""
    model_keys = set(BacktestRequest.model_fields)
    schema_keys = set(field_map())
    assert model_keys == schema_keys, (
        f"模型多出 {sorted(model_keys - schema_keys)}；schema 多出 {sorted(schema_keys - model_keys)}"
    )


def test_fill_defaults_come_from_schema():
    """normalize_config 补的默认值必须等于 schema 里登记的值（单一来源）。"""
    req = BacktestRequest(strategy_id="momentum_t", start_date="2024-01-02", end_date="2024-06-30")
    filled = {k: v for k, v in req.model_dump().items() if k in top_level_defaults()}
    assert filled == top_level_defaults()


@pytest.mark.parametrize("key", sorted(top_level_defaults()))
def test_normalize_fills_missing_key(key):
    """非模型路径（模板/实验/寻优）缺顶层字段时，normalize_config 必须补齐 schema 默认值。"""
    cfg = _base_cfg()
    cfg.pop(key, None)
    out = validate_backtest_config(cfg)
    assert out[key] == top_level_defaults()[key]


# ---------------------------------------------------------------- 注册值不能被静默覆盖


@pytest.mark.parametrize("value", [0, 3])
def test_pool_refill_min_passes_through_unchanged(value):
    """历史事故回归：pool_refill_min 曾被 pydantic 丢弃后强制兜底 2。

    0=关闭 是用户拍板值（文档/前端/引擎口径一致），必须原样透传到引擎配置。
    """
    req = BacktestRequest(strategy_id="momentum_t", start_date="2024-01-02", end_date="2024-06-30",
                          universe_auto=True, pool_refill_min=value)
    assert req.pool_refill_min == value
    out = validate_backtest_config(req.model_dump())
    assert out["pool_refill_min"] == value


@pytest.mark.parametrize("key,value", [("pool_gate", False), ("index_gate", False),
                                       ("nav_take_profit_pct", 0.0), ("auto_min_rps", 0.0),
                                       ("monthly_withdraw_base", 0.0)])
def test_explicit_falsy_value_not_overwritten_by_default(key, value):
    """显式传 0 / False 不能被默认值填充逻辑当成「缺省」覆盖（is None 而非 falsy 判断）。"""
    out = validate_backtest_config(_base_cfg(universe_auto=True, **{key: value}))
    assert out[key] == value


# ---------------------------------------------------------------- 前端渲染覆盖


def test_meta_route_is_registered():
    # endpoint must really be mounted at /api/backtests/meta (guards prefix/order typos)
    from app.main import app

    assert "/api/backtests/meta" in set(app.openapi().get("paths", {}))


def test_meta_endpoint_covers_all_ui_fields():
    meta = backtest_form_meta(_user="contract-test")
    assert {f["key"] for f in meta["fields"]} == {f["key"] for f in UI_FIELDS}


def test_ui_fields_supply_default_or_required():
    """前端要据 meta 生成 initialValues：每个可渲染字段必须有默认值或标 required。"""
    for f in UI_FIELDS:
        assert "default" in f or f.get("required"), f"{f['key']} 既无默认值也未标 required"


def test_dynamic_choice_tables_resolved():
    meta = {f["key"]: f for f in backtest_form_meta(_user="contract-test")["fields"]}
    assert [c["value"] for c in meta["auto_rank_key"]["choices"]] == ["score", "accel", "fresh", "mom_gap"]
    assert [c["value"] for c in meta["auto_boards"]["choices"]] == ["main", "chinext", "star", "bse"]
    assert [c["value"] for c in meta["auto_index"]["choices"]] == ["sz50", "hs300", "zz500", "csi800"]


@pytest.mark.skipif(not _BACKTEST_LIST_TSX.exists(), reason="前端源码不在本仓库")
def test_every_ui_field_is_rendered_by_the_form():
    """新增顶层字段却没在前端表单里出现 = 用户永远填不到，必须红灯。"""
    source = _BACKTEST_LIST_TSX.read_text(encoding="utf-8")
    rendered = set(re.findall(r'name="([a-z0-9_]+)"', source))
    missing = sorted({f["key"] for f in UI_FIELDS} - rendered)
    assert not missing, f"这些顶层字段未在 BacktestList.tsx 渲染：{missing}"