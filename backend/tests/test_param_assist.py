# -*- coding: utf-8 -*-
"""AI 参数助手（方案 A 单轮）单元测试：护栏净化 / 合并 / diff / 冲突探测。

LLM 全部 monkeypatch（app.llm.provider.chat），不打真实网络。
护栏是这份功能的安全边界：LLM 只在登记过的键与区间内开方，
越界/表外/锁定项一律丢弃（与 AiAnalysis 的幻觉护栏同口径）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import param_assist, provider  # noqa: E402


def _cfg(**over):
    cfg = {
        "name": "参数助手测试",
        "strategy_id": "momentum_t",
        "period": "daily",
        "universe": ["600000"],
        "start_date": "2024-01-02",
        "end_date": "2024-06-30",
        "initial_capital": 400000,
        "auto_with_accel": False,   # 表单恒传该键（bool_or_none 的 None 才表示"跟随策略默认"）
        "params": {},
        "risk_config": {},
    }
    cfg.update(over)
    return cfg


@pytest.fixture()
def llm(monkeypatch):
    """provider.chat 打桩：test 用例往 box["reply"] 里塞模型回复文本"""
    box = {"reply": "{}", "messages": None}

    def _fake(profile_name, messages, **kw):
        box["messages"] = messages
        return {"content": box["reply"], "model": "stub-model"}

    monkeypatch.setattr(provider, "chat", _fake)
    return box


# ---------------------------------------------------------------- 顶层字段护栏


def test_sanitize_top_clamps_drops_blocked_and_unknown():
    clean, notes = param_assist.sanitize_top(
        {"pool_gate": True, "pool_gate_enter_th": 0.99, "index_gate_ma": 3,
         "initial_capital": 400000, "period": "minute5", "no_such_field": 1},
        _cfg())
    # 越界 clamp；与原值相同剔除；未登记的键丢弃
    assert clean == {"pool_gate": True, "pool_gate_enter_th": 0.49, "index_gate_ma": 5}
    assert any("period" in n for n in notes)
    assert any("no_such_field" in n for n in notes)


def test_sanitize_top_str_enum_and_bool():
    clean, _ = param_assist.sanitize_top(
        {"benchmark": "000300", "exclude_st": "false",
         "auto_with_accel": None, "pool_gate": "开"}, _cfg())
    assert clean["benchmark"] == "000300"
    assert clean["exclude_st"] is False
    assert clean["auto_with_accel"] is None   # bool_or_none：None=跟随策略默认
    assert clean["pool_gate"] is True
    # 枚举外的取值丢弃
    assert param_assist.sanitize_top({"benchmark": "399001"}, _cfg())[0] == {}


# ---------------------------------------------------------------- 策略参数 / 风控护栏


def test_sanitize_params_clamps_and_skips_frozen():
    cfg = _cfg(strategy_id="momentum_slot", params={"mom_short": 10, "pool_n": 6})
    clean = param_assist.sanitize_params(
        {"mom_short": 999, "mom_long": 150, "no_such": 1}, cfg)
    assert clean == {"mom_short": 40}          # clamp 到 schema max
    # mom_long 是 frozen（表单锁定）-> 不给 LLM 改；no_such 表外 -> 丢弃


def test_sanitize_risk_covers_full_model_and_drops_out_of_range():
    cfg = _cfg(risk_config={"stop_loss_pct": 12.0, "max_sector_pct": 0.0,
                            "atr_period": 14})
    clean = param_assist.sanitize_risk(
        {"stop_loss_pct": 8, "max_sector_pct": 30, "regime_b_on": True,
         "trade_atr_mult": 3.5, "stop_loss_mode": "bogus", "max_holdings": 999,
         "adaptive": "trend", "atr_period": 14.0}, cfg)
    assert clean == {"stop_loss_pct": 8.0,       # 区间内
                     "max_sector_pct": 30.0,     # analyzer 白名单外的可调字段也放行
                     "regime_b_on": True,
                     "trade_atr_mult": 3.5,
                     "adaptive": "trend"}
    # stop_loss_mode 枚举非法、max_holdings 越界 -> 丢弃；atr_period 与原值相同 -> 剔除
    assert "stop_loss_mode" not in clean and "max_holdings" not in clean
    assert "atr_period" not in clean


# ---------------------------------------------------------------- 字段目录（喂给 LLM）


def test_catalog_excludes_blocked_and_frozen():
    cfg = _cfg(strategy_id="momentum_slot")
    top_keys = {r["key"] for r in param_assist._top_catalog(cfg)}
    assert "pool_gate" in top_keys
    assert "period" not in top_keys and "strategy_id" not in top_keys
    param_keys = {r["key"] for r in param_assist._param_catalog(cfg)}
    assert "mom_short" in param_keys
    assert "mom_long" not in param_keys        # frozen


def test_meta_endpoint_still_exposes_dynamic_choices():
    """dynamic_choices 抽到 schema 后，/meta 的动态选项口径不变。"""
    from app.api.backtests import backtest_form_meta

    fields = {f["key"]: f for f in backtest_form_meta("tester")["fields"]}
    assert fields["auto_rank_key"]["choices"]
    assert fields["auto_index"]["choices"]
    assert fields["auto_boards"]["choices"]


# ---------------------------------------------------------------- 端到端（打桩 LLM）


def test_assist_returns_diff_and_merged_config(llm):
    llm["reply"] = json.dumps({
        "top": {"initial_capital": 600000},
        "params": {"mom_short": 15},
        "risk_config": {"stop_loss_pct": 8},
        "notes": "提高资金并收紧止损",
        "unsupported": ["换周期请手动处理"],
    }, ensure_ascii=False)
    res = param_assist.assist(_cfg(), "资金提到 60 万，短周期动量 15，止损 8%")
    assert res["ok"] and res["changed"]
    assert res["merged_config"]["initial_capital"] == 600000
    assert res["merged_config"]["params"]["mom_short"] == 15
    assert res["merged_config"]["risk_config"]["stop_loss_pct"] == 8
    assert {d["key"] for d in res["diff"]} == {"initial_capital", "mom_short",
                                               "stop_loss_pct"}
    assert res["issues"] == []                 # 补丁未引入新冲突
    assert res["unsupported"] == ["换周期请手动处理"]
    assert res["model"] == "stub-model"


def test_assist_flags_newly_introduced_conflict(llm):
    """草稿本身合法，但补丁合并后违反业务约束 -> issues 非空（前端停用「应用」）"""
    llm["reply"] = json.dumps({"top": {"universe_auto": True}})
    res = param_assist.assist(_cfg(), "改成动态选股")
    assert res["ok"] and res["changed"]
    assert res["issues"] and "universe" in res["issues"][0]


def test_assist_ignores_preexisting_incomplete_draft(llm):
    """草稿还没选股票池（本来就通不过校验）时，不把预存在问题算作 AI 的冲突。"""
    llm["reply"] = json.dumps({"top": {"pool_gate": True}})
    res = param_assist.assist(_cfg(universe=[]), "打开池级趋势开关")
    assert res["ok"] and res["changed"]
    assert res["issues"] == []


def test_assist_reports_blocked_top_field_as_unsupported(llm):
    llm["reply"] = json.dumps({"top": {"period": "minute5"}})
    res = param_assist.assist(_cfg(), "改成分钟线")
    assert res["changed"] is False
    assert any("period" in u for u in res["unsupported"])


def test_assist_handles_unparseable_reply(llm):
    llm["reply"] = "我觉得你可以试试别的参数"
    res = param_assist.assist(_cfg(), "随便调调")
    assert res["ok"] is False and res["error"]


def test_extract_json_variants():
    assert param_assist._extract_json('```json\n{"top": {"a": 1}}\n```') == {"top": {"a": 1}}
    assert param_assist._extract_json('前言 {"top": {}} 后记') == {"top": {}}
    assert param_assist._extract_json("no json here") is None


def test_param_assist_endpoint_roundtrip(monkeypatch):
    """端点冒烟：鉴权 + 请求/响应契约（LLM 打桩，不打真实网络）。"""
    from fastapi.testclient import TestClient

    from app import config, db
    from app.main import app

    monkeypatch.setattr(provider, "db_key_entries",
                        lambda username, db_path=None: [{"id": 1}])
    monkeypatch.setattr(provider, "chat", lambda *a, **kw: {
        "content": '```json\n{"top": {"pool_gate": true}}\n```', "model": "stub"})
    config.ensure_dirs()
    db.init_db()

    with TestClient(app) as client:
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}

        r = client.post("/api/ai/param-assist",
                        json={"message": "打开池级趋势开关", "config": _cfg()},
                        headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["changed"]
        assert body["diff"] == [{"scope": "top", "key": "pool_gate",
                                 "old": None, "new": True}]
        assert body["merged_config"]["pool_gate"] is True

        # 空描述直接 400，不消耗 LLM 调用
        r = client.post("/api/ai/param-assist",
                        json={"message": "   ", "config": _cfg()}, headers=headers)
        assert r.status_code == 400
