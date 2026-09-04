# -*- coding: utf-8 -*-
"""AI 分析 v2 测试：诊断引擎 / 建议 clamp / 验证闭环 / 实盘简报点评 / 胜率统计

LLM 全部 monkeypatch（app.llm.provider.chat），不打真实网络。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.llm import commentary, diagnostics, validation  # noqa: E402
from app.llm.analyzer import _extract_suggestions, _sanitize_suggestions  # noqa: E402


# ---------------- 合成报告 ----------------

def _curve(days=120, ratio=0.6, flat_tail=0):
    """合成资金曲线：position_ratio=ratio，尾部 flat_tail 天空仓"""
    eq, out = 1_000_000.0, []
    for d in range(1, days + 1):
        eq *= 1.001
        r = 0.0 if d > days - flat_tail else ratio
        out.append({"date": f"2025-{(d - 1) // 28 + 1:02d}-{(d - 1) % 28 + 1:02d}",
                    "equity": round(eq, 2), "drawdown": 0.0, "position_ratio": r})
    return out


def _report(**over):
    rep = {
        "config": {"name": "测试", "strategy_id": "momentum_slot",
                   "params": {"mom_short": 10, "pool_n": 6, "t_mode": "grid"},
                   "risk_config": {"atr_multiplier": 2.5, "max_holdings": 3},
                   "start_date": "2025-01-01", "end_date": "2025-06-30",
                   "initial_capital": 1_000_000},
        "metrics": {
            "total_return": -0.10, "annual_return": -0.2, "max_drawdown": -0.30,
            "sharpe": -0.8, "calmar": -0.66, "win_rate": 0.45,
            "profit_loss_ratio": 0.9, "total_trades": 30, "total_pnl": -100000.0,
            "t_trade_count": 40, "t_win_rate": 0.30, "t_pnl": -5000.0,
            "add_pnl": -3000.0, "stop_loss_pnl": -60000.0,
            "commission_total": 8000.0, "start_equity": 1_000_000.0,
            "end_equity": 900_000.0,
        },
        "equity_curve": _curve(),
        "monthly_returns": [{"year": 2025, "month": 1, "return": 0.01}],
        "trade_log": [
            {"code": c, "name": f"票{c}", "time": "2025-03-01", "side": "sell",
             "type": tp, "pnl": pnl}
            for c, tp, pnl in ([("600000", "止损", -30000.0)] * 6
                               + [("000001", "加仓", -3000.0)] * 3
                               + [("600036", "开仓", 1000.0)] * 3)
        ],
    }
    rep.update(over)
    return rep


# ---------------- 诊断引擎 ----------------

def test_diagnose_t_neg_and_deep_dd_and_stop_heavy():
    findings = diagnostics.diagnose(_report())
    codes = {f["code"] for f in findings}
    assert {"T_NEG_PNL", "DEEP_DD", "STOP_HEAVY", "T_WIN_RATE_LOW",
            "ADD_DRAG"} <= codes
    sev = {f["code"]: f["severity"] for f in findings}
    assert sev["T_NEG_PNL"] == "high" and sev["DEEP_DD"] == "high"


def test_diagnose_idle_and_long_flat_and_no_trades():
    rep = _report(metrics={"total_trades": 0}, equity_curve=_curve(ratio=0.0))
    codes = {f["code"] for f in diagnostics.diagnose(rep)}
    assert "NO_TRADES" in codes

    rep2 = _report(equity_curve=_curve(ratio=0.1))
    codes2 = {f["code"] for f in diagnostics.diagnose(rep2)}
    assert "IDLE_CAPITAL" in codes2
    assert "LONG_FLAT" not in codes2  # ratio=0.1 无空仓日
    rep3 = _report(equity_curve=_curve(ratio=0.5, flat_tail=80))
    codes3 = {f["code"] for f in diagnostics.diagnose(rep3)}
    assert "LONG_FLAT" in codes3


def test_diagnose_concentration_and_overfit_and_bench():
    rep = _report()
    rep["trade_log"] = ([{"code": "600000", "name": "A", "type": "清仓", "pnl": -90000.0}]
                        + [{"code": c, "name": c, "type": "清仓", "pnl": 100.0}
                           for c in ("000001", "600036", "000002")])
    codes = {f["code"] for f in diagnostics.diagnose(rep)}
    assert "CONCENTRATION" in codes

    rep2 = _report()
    codes2 = {f["code"] for f in diagnostics.diagnose(rep2, {"mom_short": 0.9})}
    assert "OVERFIT_WARN" in codes2

    rep3 = _report(metrics={**_report()["metrics"],
                            "excess_return": -0.10, "benchmark_return": 0.15})
    rep3["benchmark"] = {"index_key": "000905", "name": "中证500", "curve": [], "return": 0.15}
    codes3 = {f["code"] for f in diagnostics.diagnose(rep3)}
    assert "UNDERPERFORM_BENCH" in codes3


def test_diagnose_healthy_report_empty():
    rep = _report(metrics={"total_return": 0.3, "annual_return": 0.6,
                           "max_drawdown": -0.10, "sharpe": 1.5, "calmar": 6.0,
                           "win_rate": 0.55, "profit_loss_ratio": 1.8,
                           "total_trades": 25, "total_pnl": 300000.0,
                           "t_trade_count": 0, "add_pnl": 5000.0,
                           "stop_loss_pnl": -5000.0, "commission_total": 2000.0,
                           "start_equity": 1_000_000.0, "end_equity": 1_300_000.0},
                  equity_curve=_curve(ratio=0.7))
    rep["trade_log"] = [{"code": "600000", "name": "A", "type": "清仓", "pnl": 50000.0}]
    findings = diagnostics.diagnose(rep)
    high = [f for f in findings if f["severity"] == "high"]
    assert high == []


# ---------------- 建议 clamp（幻觉护栏） ----------------

def test_sanitize_clamps_and_drops():
    rep = _report()
    data = {"params": {"mom_short": 999, "pool_n": 8, "unknown_key": 3,
                       "t_mode": "grid|网格（双止损）", "w_short": "bad"},
            "risk_config": {"atr_multiplier": "not-a-number", "stop_loss_mode": "whatever",
                            "max_holdings": 3, "cash_reserve_pct": 2.0,
                            "not_a_risk_field": 1}}
    sug = _sanitize_suggestions(data, rep)
    assert sug is not None
    # 越界 clamp 到 schema max
    assert sug["params"]["mom_short"] == 40
    assert sug["params"]["pool_n"] == 8
    assert "unknown_key" not in sug["params"]
    # categorical 只认 | 前的 value；与原值相同 → 剔除
    assert "t_mode" not in sug["params"]
    assert "w_short" not in sug["params"]
    # risk：非法枚举/非数值丢弃；与原值相同剔除
    assert "atr_multiplier" not in sug["risk_config"]
    assert "stop_loss_mode" not in sug["risk_config"]
    assert "max_holdings" not in sug["risk_config"]  # 原值 3，相同剔除
    assert sug["risk_config"]["cash_reserve_pct"] == 2.0
    assert "not_a_risk_field" not in sug["risk_config"]


def test_sanitize_same_value_dropped_and_empty_none():
    rep = _report()
    assert _sanitize_suggestions({"params": {"mom_short": 10},
                                  "risk_config": {}}, rep) is None
    sug = _sanitize_suggestions({"params": {"mom_short": 20}, "risk_config": {}}, rep)
    assert sug == {"params": {"mom_short": 20}, "risk_config": {}}


def test_extract_suggestions_from_markdown():
    rep = _report()
    content = "## 诊断解读\n有问题。\n```json\n" \
              '{"params": {"mom_short": 999}, "risk_config": {"atr_multiplier": 3.0}}\n```'
    body, sug = _extract_suggestions(content, rep)
    assert "```json" not in body
    assert sug["params"]["mom_short"] == 40  # clamp
    assert sug["risk_config"]["atr_multiplier"] == 3.0


# ---------------- 验证闭环 ----------------

def test_merge_suggestions():
    cfg = {"name": "A", "params": {"mom_short": 10}, "risk_config": {"atr_multiplier": 2.5}}
    out = validation.merge_suggestions(cfg, {"params": {"pool_n": 8},
                                             "risk_config": {"max_holdings": 4}})
    assert out["params"] == {"mom_short": 10, "pool_n": 8}
    assert out["risk_config"] == {"atr_multiplier": 2.5, "max_holdings": 4}
    # 深拷贝：原配置不被污染
    assert cfg["params"] == {"mom_short": 10}


def test_compare_metrics_verdicts():
    orig = {"total_return": 0.10, "sharpe": 1.0, "calmar": 0.5,
            "max_drawdown": -0.20, "win_rate": 0.5}
    better = {"total_return": 0.15, "sharpe": 1.2, "calmar": 0.8,
              "max_drawdown": -0.15, "win_rate": 0.55}
    c = validation.compare_metrics(orig, better)
    assert c["verdict"] == "改善"

    # 总收益显著下跌 → 恶化
    c2 = validation.compare_metrics(orig, {**orig, "total_return": 0.0, "sharpe": 1.1})
    assert c2["verdict"] == "恶化"

    # 收益改善但回撤显著加深 → 不算改善
    c3 = validation.compare_metrics(orig, {**orig, "total_return": 0.20,
                                           "max_drawdown": -0.35})
    assert c3["verdict"] == "持平"

    # 单项小幅变好 → 持平
    c4 = validation.compare_metrics(orig, {**orig, "sharpe": 1.05})
    assert c4["verdict"] == "持平"


def test_run_validation_backtest_synthetic(demo_env, monkeypatch):
    """monkeypatch runner.run_backtest，验证合并/对比/缓存清理全链路（不真跑引擎）"""
    from app.engine import datafeed
    data_dir, start, end = demo_env

    def fake_run(cfg, data_dir=None, progress_cb=None):
        assert cfg["params"]["mom_short"] == 40
        assert cfg["name"].endswith("-AI验证")
        return {"metrics": {"total_return": 0.2, "sharpe": 1.5,
                            "max_drawdown": -0.12, "calmar": 1.6, "win_rate": 0.6}}

    monkeypatch.setattr("app.engine.runner.run_backtest", fake_run)
    cleared = {"n": 0}
    monkeypatch.setattr(datafeed, "clear_cache", lambda: cleared.__setitem__("n", cleared["n"] + 1))
    rep = _report()
    # 建议 object 已在 analyzer 层 sanitize（真实链路口径），此处直接传合法值
    out = validation.run_validation_backtest(
        rep["config"], {"params": {"mom_short": 40}},
        rep["metrics"], data_dir=data_dir)
    assert out["comparison"]["verdict"] == "改善"
    assert out["config_diff"]["params.mom_short"] == {"old": 10, "new": 40}
    assert out["metrics"]["orig"]["total_return"] == -0.10
    assert cleared["n"] == 1


def test_review_commentary_best_effort(demo_env, monkeypatch):
    import app.llm.provider as provider

    monkeypatch.setattr(provider, "chat",
                        lambda *a, **k: {"content": "采纳。收益+5pct。", "model": "m"})
    out = validation.review_commentary({}, {"metrics": {}, "comparison": {},
                                            "config_diff": {}})
    assert out == "采纳。收益+5pct。"

    def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(provider, "chat", boom)
    assert validation.review_commentary({}, {}) is None


# ---------------- 实盘简报/点评 ----------------

def test_premarket_briefing_mock(monkeypatch):
    import app.llm.provider as provider
    monkeypatch.setattr(provider, "chat",
                        lambda *a, **k: {"content": "- gate 正常\n- 无持仓", "model": "m"})
    text = commentary.premarket_briefing({"as_of": "2025-06-30", "gate_state": 0,
                                          "positions": 0, "signals": [], "warns": []})
    assert text and "gate" in text

    def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(provider, "chat", boom)
    assert commentary.premarket_briefing({"as_of": "2025-06-30"}) is None
    assert commentary.postclose_commentary({"date": "2025-06-30"}, []) is None


# ---------------- 胜率统计 ----------------

def test_ai_verdict_stats(tmp_path):
    db_path = str(tmp_path / "meta.db")
    db.init_db(db_path)
    for validation_json in ({"verdict": "改善"}, {"verdict": "改善"},
                            {"verdict": "恶化"}, {"error": "boom"}):
        db.save_analysis("t", "bt", "p", "m", "success", None, 1, 1.0, None,
                         diagnostics=None, validation=validation_json, db_path=db_path)
    stats = db.ai_verdict_stats(db_path)
    assert stats["total"] == 3 and stats["改善"] == 2 and stats["恶化"] == 1
    assert stats["error"] == 1
    assert stats["improved_rate"] == pytest.approx(2 / 3, abs=1e-3)


def test_list_analyses_roundtrip(tmp_path):
    db_path = str(tmp_path / "meta.db")
    db.init_db(db_path)
    diag = [{"code": "DEEP_DD", "severity": "high", "title": "t", "evidence": "e", "hint": "h"}]
    val = {"verdict": "改善", "comparison": {"rows": []}}
    db.save_analysis("task1", "bt1", "deepseek", "m", "success", "内容", 10, 1.0, None,
                     suggestions={"params": {"pool_n": 8}, "risk_config": {}},
                     diagnostics=diag, validation=val, db_path=db_path)
    rows = db.list_analyses("bt1", db_path)
    assert rows[0]["diagnostics"] == diag
    assert rows[0]["validation"]["verdict"] == "改善"
    assert rows[0]["suggestions"]["params"]["pool_n"] == 8


# ---------------- analyzer 端到端（mock chat） ----------------

def test_analyze_backtest_end_to_end_mock(monkeypatch):
    import app.llm.analyzer as analyzer
    rep = _report()
    md = ("## 诊断解读\n做T为负。\n\n```json\n"
          '{"params": {"mom_short": 999, "pool_n": 8}, '
          '"risk_config": {"atr_multiplier": 3.0}}\n```')

    def fake_chat(profile, messages, temperature=0.3, db_path=None, username=None,
                  tools=None):
        assert any("规则引擎" in m["content"] or "findings" in m["content"]
                   for m in messages if m["role"] == "user")
        return {"content": md, "model": "mock", "tokens": 100, "elapsed": 0.5,
                "profile": "deepseek", "tool_calls": []}

    monkeypatch.setattr(analyzer, "chat", fake_chat)
    out = analyzer.analyze_backtest(rep, param_importance={"mom_short": 0.9})
    # 诊断 findings 注入且随结果返回
    codes = {f["code"] for f in out["diagnostics"]}
    assert "T_NEG_PNL" in codes and "OVERFIT_WARN" in codes
    # 建议 clamp 生效
    assert out["suggestions"]["params"]["mom_short"] == 40
    assert out["suggestions"]["params"]["pool_n"] == 8
    assert out["suggestions"]["risk_config"]["atr_multiplier"] == 3.0
    # json 块从正文移除
    assert "```json" not in out["content"]


# ---------------- 下钻工具（方案 A） ----------------

def test_query_trades_groups_and_filters():
    from app.llm import drilldown
    rep = _report()
    out = drilldown.query_trades(rep, group_by="code")
    groups = {g["group"]: g for g in out["groups"]}
    assert groups["600000"]["pnl"] == -180000.0 and groups["600000"]["n"] == 6
    # 亏损组排前
    assert [g["group"] for g in out["groups"]][0] == "600000"
    assert out["overall"]["n"] == 12
    assert out["overall"]["win_rate"] == 0.25

    out2 = drilldown.query_trades(rep, group_by="type", code="600000")
    assert out2["overall"]["pnl"] == -180000.0
    assert out2["groups"][0]["group"] == "止损"

    out3 = drilldown.query_trades(rep, group_by="month", month="2099-01")
    assert out3["overall"]["n"] == 0

    assert "error" in drilldown.query_trades(rep, group_by="bogus")


def test_get_code_profile_and_errors():
    from app.llm import drilldown
    rep = _report()
    out = drilldown.get_code_profile(rep, "600000")
    assert out["summary"]["n_trades"] == 6
    assert out["summary"]["closed_pnl"] == -180000.0
    assert out["summary"]["types"] == {"止损": 6}
    assert len(out["trades"]) == 6
    assert out["周线收盘(回测区间)"] is None  # data_dir=None 静默降级
    assert "error" in drilldown.get_code_profile(rep, "999999")


def test_get_market_context():
    from app.llm import drilldown
    ec = [{"date": "2025-01-01", "equity": 100, "drawdown": 0.0, "position_ratio": 0.5},
          {"date": "2025-01-15", "equity": 90, "drawdown": -0.1, "position_ratio": 0.4},
          {"date": "2025-02-01", "equity": 95, "drawdown": -0.05, "position_ratio": 0.6},
          {"date": "2025-02-20", "equity": 110, "drawdown": 0.0, "position_ratio": 0.7}]
    bench = [{"date": "2025-01-01", "equity": 100}, {"date": "2025-01-15", "equity": 98},
             {"date": "2025-02-01", "equity": 99}, {"date": "2025-02-20", "equity": 102}]
    out = drilldown.get_market_context({"equity_curve": ec,
                                        "benchmark": {"curve": bench}})
    assert out["period"]["strategy_ret"] == 0.10
    assert out["period"]["bench_ret"] == 0.02
    monthly = {m["month"]: m for m in out["monthly"]}
    assert monthly["2025-01"]["strategy_ret"] == -0.10
    assert monthly["2025-01"]["bench_ret"] == -0.02
    assert monthly["2025-01"]["avg_position_ratio"] == 0.45
    eps = out["回撤谷(最深前5)"]
    assert len(eps) == 1 and eps[0]["depth"] == -0.1 and eps[0]["trough"] == "2025-01-15"
    # 区间切片
    out2 = drilldown.get_market_context({"equity_curve": ec,
                                         "benchmark": {"curve": bench}},
                                        start_month="2025-02")
    assert out2["period"]["start"] == "2025-02-01"
    assert "error" in drilldown.get_market_context({"equity_curve": []})


def test_execute_tool_dispatch():
    from app.llm import drilldown
    rep = _report()
    out = drilldown.execute_tool("query_trades", {"group_by": "code"}, rep)
    assert out["overall"]["n"] == 12
    assert "error" in drilldown.execute_tool("nope", {}, rep)
    assert "error" in drilldown.execute_tool("get_code_profile", {}, rep)
    assert "error" in drilldown.execute_tool("get_code_profile", {"code": "999999"}, rep)


def _tool_call(cid, name, args_json):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args_json}}


def test_agentic_loop_drills_then_answers(monkeypatch):
    import app.llm.analyzer as analyzer
    rep = _report()
    final_md = ("## 诊断解读\n查过了。\n```json\n"
                '{"params": {"pool_n": 8}, "risk_config": {}}\n```')
    seen_tool_msg = {"hit": False}

    def fake_chat(profile, messages, temperature=0.3, db_path=None, username=None,
                  tools=None):
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if tools and not has_tool_result:  # 首轮：请求下钻
            return {"content": "", "model": "m", "tokens": 1, "elapsed": 0.1,
                    "profile": "p",
                    "tool_calls": [_tool_call("t1", "query_trades",
                                              '{"group_by": "code"}')]}
        # 收尾轮（拿到工具结果后给出最终回答）：检查工具结果已回喂
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            seen_tool_msg["hit"] = '"groups"' in tool_msgs[0]["content"]
        return {"content": final_md, "tool_calls": [], "model": "m",
                "tokens": 2, "elapsed": 0.1, "profile": "p"}

    monkeypatch.setattr(analyzer, "chat", fake_chat)
    out = analyzer.analyze_backtest(rep)
    assert out["suggestions"]["params"]["pool_n"] == 8
    assert out["tool_trace"] == [{"name": "query_trades", "args": {"group_by": "code"}}]
    assert "下钻取证 1 次：query_trades×1" in out["content"]
    assert seen_tool_msg["hit"]


def test_agentic_loop_fallback_when_tools_unsupported(monkeypatch):
    import app.llm.analyzer as analyzer
    from app.llm.provider import LLMError
    rep = _report()
    md = "## 诊断解读\n不支持工具也能分析。\n```json\n" \
         '{"params": {"pool_n": 7}, "risk_config": {}}\n```'

    def fake_chat(profile, messages, temperature=0.3, db_path=None, username=None,
                  tools=None):
        if tools:
            raise LLMError("Key 池全部条目不可用（最后错误: 400 tools not supported）")
        return {"content": md, "model": "m", "tokens": 1, "elapsed": 0.1,
                "profile": "p", "tool_calls": []}

    monkeypatch.setattr(analyzer, "chat", fake_chat)
    out = analyzer.analyze_backtest(rep)
    assert out["suggestions"]["params"]["pool_n"] == 7
    assert out["tool_trace"] == []


def test_agentic_loop_budget_cap(monkeypatch):
    import app.llm.analyzer as analyzer
    rep = _report()
    final_md = "结论。\n```json\n{\"params\": {}, \"risk_config\": {}}\n```"
    calls = {"n": 0}

    def fake_chat(profile, messages, temperature=0.3, db_path=None, username=None,
                  tools=None):
        calls["n"] += 1
        if tools:  # 每轮都要求 3 次下钻（逼出总次数护栏）
            base = calls["n"] * 10
            return {"content": "", "model": "m", "tokens": 1, "elapsed": 0.1,
                    "profile": "p",
                    "tool_calls": [_tool_call(f"t{base + i}", "query_trades",
                                              '{"group_by": "month"}')
                                   for i in range(3)]}
        return {"content": final_md, "tool_calls": [], "model": "m",
                "tokens": 2, "elapsed": 0.1, "profile": "p"}

    monkeypatch.setattr(analyzer, "chat", fake_chat)
    out = analyzer.analyze_backtest(rep)
    assert len(out["tool_trace"]) == analyzer.MAX_TOOL_CALLS_TOTAL  # 10 次封顶
    assert "结论。" in out["content"]  # 轮次耗尽后强制无工具收尾生效
    assert out["suggestions"] is None  # 空建议块净化为 None（符合约定）
    assert calls["n"] == 5  # 4 轮下钻(3+3+3+1) + 1 次强制收尾
