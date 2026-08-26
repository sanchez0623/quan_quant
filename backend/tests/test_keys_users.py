# -*- coding: utf-8 -*-
"""Key 管理 + 多用户隔离 + 用户管理 API 测试"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.main import app  # noqa: E402

TEST_USER = "friend_a"
TEST_USER2 = "friend_b"


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    config.ensure_dirs()
    db.init_db()
    # 准备两个测试用户（幂等）
    for u in (TEST_USER, TEST_USER2):
        if not db.get_user(u):
            db.create_user(u, hash_password("pass123456"))
    # 清理旧测试 key
    for u in (TEST_USER, TEST_USER2, config.ADMIN_USERNAME):
        for k in db.list_llm_keys(u):
            db.delete_llm_key(k["id"], u)
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def tokens(client):
    return {"a": _login(client, TEST_USER, "pass123456"),
            "b": _login(client, TEST_USER2, "pass123456"),
            "admin": _login(client, "admin", config.ADMIN_PASSWORD or "admin123")}


# ---------------- Key CRUD ----------------

def test_key_crud_and_masking(client, tokens):
    # 新增
    r = client.post("/api/keys", headers=H(tokens["a"]), json={
        "provider": "deepseek", "api_key": "sk-test-abcdef123456",
        "label": "我的DS", "sort_order": 1})
    assert r.status_code == 200, r.text
    key_id = r.json()["id"]
    # 列表：脱敏、不含明文
    r = client.get("/api/keys", headers=H(tokens["a"]))
    keys = r.json()["keys"]
    assert len(keys) == 1
    assert "sk-test-abcdef123456" not in r.text          # 明文绝不出现
    assert keys[0]["api_key"].startswith("sk-") and "***" in keys[0]["api_key"]
    assert keys[0]["provider"] == "deepseek" and keys[0]["label"] == "我的DS"
    # registry 返回内置服务商
    assert "openrouter" in r.json()["providers"] and "volc" in r.json()["registry"]
    # 修改：换 key + 改备注 + 禁用
    r = client.put(f"/api/keys/{key_id}", headers=H(tokens["a"]),
                   json={"api_key": "sk-new-key-9999xyz", "label": "DS备用", "enabled": False})
    assert r.status_code == 200
    r = client.get("/api/keys", headers=H(tokens["a"]))
    assert r.json()["keys"][0]["label"] == "DS备用"
    assert r.json()["keys"][0]["enabled"] is False
    # 删除
    r = client.delete(f"/api/keys/{key_id}", headers=H(tokens["a"]))
    assert r.status_code == 200
    assert client.get("/api/keys", headers=H(tokens["a"])).json()["keys"] == []


def test_key_validation(client, tokens):
    # 未知服务商
    r = client.post("/api/keys", headers=H(tokens["a"]),
                    json={"provider": "not_exist", "api_key": "sk-12345678"})
    assert r.status_code == 400
    # custom 缺 base_url/model
    r = client.post("/api/keys", headers=H(tokens["a"]),
                    json={"provider": "custom", "api_key": "sk-12345678"})
    assert r.status_code == 400
    # key 过短
    r = client.post("/api/keys", headers=H(tokens["a"]),
                    json={"provider": "deepseek", "api_key": "short"})
    assert r.status_code == 422


# ---------------- 多用户隔离 ----------------

def test_user_key_isolation(client, tokens):
    # a 加两条 key
    ids = []
    for i, prov in enumerate(("deepseek", "openrouter"), start=1):
        r = client.post("/api/keys", headers=H(tokens["a"]),
                        json={"provider": prov, "api_key": f"sk-a-key-{i}00000",
                              "label": f"A{i}", "sort_order": i})
        ids.append(r.json()["id"])
    # b 加一条
    r = client.post("/api/keys", headers=H(tokens["b"]),
                    json={"provider": "volc", "api_key": "sk-b-key-999000", "label": "B1"})
    b_id = r.json()["id"]
    # a 只看到自己的 2 条
    r = client.get("/api/keys", headers=H(tokens["a"]))
    assert len(r.json()["keys"]) == 2
    assert all("sk-b-key" not in str(k) for k in r.json()["keys"])
    # b 只看到自己的 1 条
    r = client.get("/api/keys", headers=H(tokens["b"]))
    assert len(r.json()["keys"]) == 1
    # b 改/删 a 的 key → 404（属主校验）
    assert client.put(f"/api/keys/{ids[0]}", headers=H(tokens["b"]),
                      json={"label": "hack"}).status_code == 404
    assert client.delete(f"/api/keys/{ids[0]}", headers=H(tokens["b"])).status_code == 404
    # profiles 接口：b 的 user_key_pool 只有 volc 条目
    r = client.get("/api/ai/profiles", headers=H(tokens["b"]))
    assert r.json()["mode"] == "db_key_pool"
    assert len(r.json()["user_key_pool"]) == 1
    assert r.json()["user_key_pool"][0]["provider"] == "volc"
    # 清理
    for kid in ids:
        client.delete(f"/api/keys/{kid}", headers=H(tokens["a"]))
    client.delete(f"/api/keys/{b_id}", headers=H(tokens["b"]))


# ---------------- 用户管理（仅 admin） ----------------

def test_user_management_admin_only(client, tokens):
    # 普通用户访问 → 403
    assert client.get("/api/users", headers=H(tokens["a"])).status_code == 403
    assert client.post("/api/users", headers=H(tokens["a"]),
                       json={"username": "x1", "password": "1234567"}).status_code == 403
    # admin 列表含测试用户
    r = client.get("/api/users", headers=H(tokens["admin"]))
    assert r.status_code == 200
    names = [u["username"] for u in r.json()]
    assert {"admin", TEST_USER, TEST_USER2} <= set(names)


def test_user_create_delete_flow(client, tokens):
    admin_h = H(tokens["admin"])
    # 创建
    r = client.post("/api/users", headers=admin_h,
                    json={"username": "tmp_user", "password": "tmp123456"})
    assert r.status_code == 200
    # 重复创建 -> 400
    assert client.post("/api/users", headers=admin_h,
                       json={"username": "tmp_user", "password": "tmp123456"}).status_code == 400
    # 新用户能登录、能加 key
    t = _login(client, "tmp_user", "tmp123456")
    assert client.post("/api/keys", headers=H(t),
                       json={"provider": "deepseek", "api_key": "sk-tmp-99999xx"}).status_code == 200
    # 删除用户 -> 其 key 级联删除，无法再登录
    assert client.delete("/api/users/tmp_user", headers=admin_h).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "tmp_user", "password": "tmp123456"}).status_code == 401
    assert db.list_llm_keys("tmp_user") == []
    # 不能删 admin
    assert client.delete("/api/users/admin", headers=admin_h).status_code == 400


# ---------------- 回测配置模板 ----------------

def _demo_cfg(name="模板测试"):
    return {"name": name, "strategy_id": "ma_cross",
            "params": {"fast": 5, "slow": 20}, "risk_config": {"stop_loss_pct": 8},
            "universe": ["600000"], "start_date": "2024-01-01", "end_date": "2024-06-30",
            "period": "daily", "initial_capital": 1000000}


def test_template_crud_and_isolation(client, tokens):
    a_h, b_h, admin_h = H(tokens["a"]), H(tokens["b"]), H(tokens["admin"])
    # 创建
    r = client.post("/api/backtests/templates", headers=a_h,
                    json={"name": "A的标准配置", "config": _demo_cfg()})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    # 缺 strategy_id -> 400
    bad = _demo_cfg(); bad["strategy_id"] = ""
    assert client.post("/api/backtests/templates", headers=a_h,
                       json={"name": "x", "config": bad}).status_code == 400
    # 列表只看自己的
    assert client.post("/api/backtests/templates", headers=b_h,
                       json={"name": "B的配置", "config": _demo_cfg()}).status_code == 200
    a_list = client.get("/api/backtests/templates", headers=a_h).json()
    assert len(a_list) == 1 and a_list[0]["name"] == "A的标准配置"
    assert a_list[0]["config"]["strategy_id"] == "ma_cross"
    assert len(client.get("/api/backtests/templates", headers=b_h).json()) == 1
    # b 删 a 的模板 -> 404（属主校验）
    assert client.delete(f"/api/backtests/templates/{tid}", headers=b_h).status_code == 404
    # a 删自己的 -> ok
    assert client.delete(f"/api/backtests/templates/{tid}", headers=a_h).status_code == 200
    assert len(client.get("/api/backtests/templates", headers=a_h).json()) == 0
    # 清理 b
    b_id = client.get("/api/backtests/templates", headers=b_h).json()[0]["id"]
    client.delete(f"/api/backtests/templates/{b_id}", headers=b_h)
    _ = admin_h  # admin 未参与模板操作


# ---------------- LLM 用量统计清空 ----------------

def test_clear_llm_usage(client, tokens):
    a_h = H(tokens["a"])
    db.record_llm_usage("deepseek", "deepseek-v4-flash", 100, 50, 0.5)
    stats = client.get("/api/ai/profiles", headers=a_h).json()["usage"]
    assert stats["total_calls"] >= 1
    assert client.delete("/api/ai/usage", headers=a_h).status_code == 200
    stats = client.get("/api/ai/profiles", headers=a_h).json()["usage"]
    assert stats["total_tokens"] == 0 and stats["total_calls"] == 0
    assert stats["by_profile"] == {}
