# -*- coding: utf-8 -*-
"""API 冒烟测试：TestClient 走完整任务链路（进程池）
注意：测试前清除 LLM 相关环境变量，确保 AI 分析走“未配置”分支。
"""
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_LLM_ENV_KEYS = ("SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "OLLAMA_API_KEY",
                 "LLM_KEY", "LLM_KEY_1") + tuple(f"LLM_KEY_{i}" for i in range(2, 10))

# 必须在 import app 之前清除 LLM key（含 key 池变量），保证 AI 分析测试确定性地走无 key 分支
for _k in _LLM_ENV_KEYS:
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.main import app  # noqa: E402

# import config 已触发 _load_dotenv()：.env 中的占位 key（如 OLLAMA_API_KEY=ollama）
# 会被重新注入 os.environ，需再次清理才能保持无 key 状态
for _k in _LLM_ENV_KEYS:
    os.environ.pop(_k, None)


def _wait_task(client: Optional = None, task_id: str = "", timeout: float = 240.0,
               token: str = "") -> dict:
    """轮询任务状态至终态"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/backtests/{task_id}/status", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("success", "failed", "cancelled"):
            return data
        time.sleep(0.5)
    raise TimeoutError(f"任务 {task_id} 超时未完成")


@pytest.fixture(scope="module")
def client():
    config.ensure_dirs()
    with TestClient(app) as c:  # 触发 lifespan：init db / admin / TaskManager
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["expires_in"] == 86400
    assert data["username"] == "admin"
    return data["token"]


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------- 认证 ----------------

def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"


def test_auth_required(client):
    assert client.get("/api/strategies").status_code == 401
    assert client.get("/api/backtests").status_code == 401


# ---------------- 基础接口 ----------------

def test_strategies(client, token):
    r = client.get("/api/strategies", headers=H(token))
    assert r.status_code == 200
    data = r.json()
    ids = [s["id"] for s in data]
    assert "ma_cross" in ids and "grid_t" in ids
    for s in data:
        assert set(s) >= {"id", "name", "description", "periods", "param_schema"}
        for p in s["param_schema"]:
            assert {"key", "label", "type", "default"} <= set(p)


def test_data_demo_then_stocks(client, token):
    r = client.post("/api/data/demo", json={"stocks": ["600000", "000001", "600036"],
                                            "days": 320}, headers=H(token))
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    st = _wait_task(client, task_id, token=token)
    assert st["status"] == "success", st
    # 股票查询
    r = client.get("/api/stocks", params={"keyword": "600", "limit": 20}, headers=H(token))
    assert r.status_code == 200
    rows = r.json()
    assert any(x["code"] == "600000" for x in rows)
    assert all({"code", "name", "st"} <= set(x) for x in rows)
    # 数据状态
    r = client.get("/api/data/status", headers=H(token))
    assert r.status_code == 200
    status = r.json()
    assert status["daily"] and status["daily"]["stocks"] >= 3
    assert isinstance(status["sources"], list)


def test_backtest_validation(client, token):
    # 策略不存在
    r = client.post("/api/backtests", headers=H(token), json={
        "strategy_id": "nope", "universe": ["600000"],
        "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert r.status_code == 400
    # universe 为空
    r = client.post("/api/backtests", headers=H(token), json={
        "strategy_id": "ma_cross", "universe": [],
        "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert r.status_code == 400
    # period 不支持
    r = client.post("/api/backtests", headers=H(token), json={
        "strategy_id": "grid_t", "universe": ["600000"], "period": "weekly",
        "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert r.status_code == 400
    # 日期不合法
    r = client.post("/api/backtests", headers=H(token), json={
        "strategy_id": "ma_cross", "universe": ["600000"],
        "start_date": "2021-01-01", "end_date": "2020-01-01"})
    assert r.status_code == 400


# ---------------- 回测全链路 ----------------

def test_backtest_flow(client, token):
    r = client.post("/api/backtests", headers=H(token), json={
        "name": "API冒烟回测", "strategy_id": "ma_cross",
        "params": {"fast": 5, "slow": 10},
        "risk_config": {"max_position_pct_per_stock": 30},
        "universe": ["600000", "000001", "600036"],
        "start_date": "2024-01-01", "end_date": "2030-12-31",
        "period": "daily", "initial_capital": 1_000_000})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    assert task_id.startswith("bt_")
    # 列表
    r = client.get("/api/backtests", headers=H(token))
    assert any(t["task_id"] == task_id for t in r.json())
    # 轮询至成功
    st = _wait_task(client, task_id, token=token)
    assert st["status"] == "success", st
    assert st["progress"] == 100
    # report
    r = client.get(f"/api/backtests/{task_id}/report", headers=H(token))
    assert r.status_code == 200
    report = r.json()
    for key in ("task_id", "name", "config", "metrics", "equity_curve",
                "monthly_returns", "trade_log", "position_snapshots"):
        assert key in report
    # 参数默认值填充回显
    assert "max_adds" in report["config"]["params"]
    # kline
    code = report["config"]["universe"][0]
    r = client.get(f"/api/backtests/{task_id}/kline",
                   params={"code": code}, headers=H(token))
    assert r.status_code == 200
    kline = r.json()
    assert kline["code"] == code
    assert kline["bars"]
    assert {"date", "open", "high", "low", "close", "volume"} <= set(kline["bars"][0])
    if kline["marks"]:
        assert {"time", "price", "side", "type", "trade_id"} <= set(kline["marks"][0])
    # 状态 404
    r = client.get("/api/backtests/bt_notexist/status", headers=H(token))
    assert r.status_code == 404


def test_report_before_finish(client, token):
    r = client.get("/api/backtests/bt_notexist/report", headers=H(token))
    assert r.status_code == 404
    # 对非 success 任务（failed/pending）返回 400：构造一个 pending 任务后立即查
    r = client.post("/api/optimize", headers=H(token), json={
        "name": "寻优冒烟", "backtest_config": {
            "strategy_id": "ma_cross", "universe": ["600000", "000001", "600036"],
            "start_date": "2024-01-01", "end_date": "2030-12-31",
            "period": "daily", "params": {"fast": 5, "slow": 10}},
        "param_space": {"fast": {"type": "int", "low": 3, "high": 8}},
        "n_trials": 4, "metric": "annual_return"})
    assert r.status_code == 200, r.text
    opt_id = r.json()["task_id"]
    st = _wait_task(client, opt_id, timeout=300, token=token)
    assert st["status"] == "success", st
    r = client.get(f"/api/optimize/{opt_id}", headers=H(token))
    assert r.status_code == 200
    detail = r.json()
    for key in ("task_id", "status", "metric", "n_trials", "best_params",
                "best_value", "trials", "param_importance", "oos_validation", "error"):
        assert key in detail
    assert detail["status"] == "success"
    assert detail["best_params"] and "fast" in detail["best_params"]
    assert detail["oos_validation"]["overfit_risk"] in ("high", "medium", "low")
    r = client.get("/api/optimize", headers=H(token))
    assert any(t["task_id"] == opt_id for t in r.json())


# ---------------- AI 分析（无 key 环境） ----------------

def test_ai_no_key_fails_with_friendly_error(client, token):
    r = client.get("/api/ai/profiles", headers=H(token))
    assert r.status_code == 200
    prof = r.json()
    assert {"profiles", "default", "usage", "user_key_pool", "providers"} <= set(prof)
    assert all(p["available"] is False for p in prof["profiles"])  # 环境已清空 key
    assert prof["user_key_pool"] == []  # admin 未配置 DB key
    # 找一个成功的回测
    r = client.get("/api/backtests", headers=H(token))
    success_bt = next((t["task_id"] for t in r.json() if t["status"] == "success"), None)
    assert success_bt, "前置回测应已成功"
    r = client.post("/api/ai/analyze", headers=H(token),
                    json={"backtest_id": success_bt, "profile": "main"})
    assert r.status_code == 400, "无任何 key 时应直接友好报错（不建任务）"
    assert "未配置 LLM API Key" in r.json()["detail"]
    assert "Key 管理" in r.json()["detail"]


def test_data_update_no_source_friendly_error(monkeypatch):
    """测试「无可用数据源」时的友好报错。
    仅在无数据源环境运行；若已安装 baostock/akshare/mootdx 则跳过（因测试需模拟无源环境）。"""
    import importlib.util
    # 检查是否有任一可选数据源已安装
    has_any_source = any(
        importlib.util.find_spec(pkg) is not None
        for pkg in ("baostock", "akshare", "mootdx", "pytdx")
    )
    if has_any_source:
        pytest.skip("已安装数据源，跳过「无数据源」友好报错测试（需无源环境）")

    # 无数据源环境：直接测试（不需 mock）
    from fastapi.testclient import TestClient
    from app import config, db
    from app.main import app
    config.ensure_dirs()
    db.init_db()

    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        token = r.json()["token"]
        h = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/data/update", json={"scope": "daily"}, headers=h)
        assert r.status_code == 200
        task_id = r.json()["task_id"]
        st = _wait_task(client, task_id, token=token)
        assert st["status"] == "failed"
        assert "demo" in (st["error"] or "") or "数据源" in (st["error"] or "")
