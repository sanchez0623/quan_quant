# -*- coding: utf-8 -*-
"""LLM 统一 Key 池（跨服务商无缝切换）单元测试（mock 网络调用）"""
import httpx
import pytest

from app.llm import provider


@pytest.fixture()
def mixed_pool(monkeypatch):
    """模拟 .env 配置异构 key 池：deepseek + openrouter + 火山方舟"""
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-ds-1")
    monkeypatch.setenv("LLM_KEY_2", "deepseek|deepseek-v4-pro|sk-ds-2")  # 同服务商第二条目+指定模型
    monkeypatch.setenv("LLM_KEY_3", "openrouter|sk-or-3")
    monkeypatch.setenv("LLM_KEY_4", "volc|doubao-seed-1.6|sk-volc-4")
    for i in range(5, 10):
        monkeypatch.delenv(f"LLM_KEY_{i}", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY_1", raising=False)
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-ds-1")
    # 清掉旧 profiles 变量，确保 key 池模式优先
    for env in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_2", "DEEPSEEK_API_KEY_3",
                "ZHIPU_API_KEY", "OLLAMA_API_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture()
def no_pool(monkeypatch):
    """清空全部 LLM 配置"""
    monkeypatch.delenv("LLM_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY_1", raising=False)
    for i in range(2, 10):
        monkeypatch.delenv(f"LLM_KEY_{i}", raising=False)
    for env in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_2", "DEEPSEEK_API_KEY_3",
                "SILICONFLOW_API_KEY", "ZHIPU_API_KEY", "OLLAMA_API_KEY"):
        monkeypatch.delenv(env, raising=False)


def _http_err(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.com/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def _ok(model):
    return {"content": f"ok-{model}", "model": model,
            "prompt_tokens": 10, "completion_tokens": 5, "elapsed": 0.1}


# ---------------- Key 池解析 ----------------

def test_parse_key_pool_mixed_providers(mixed_pool):
    pool = provider.parse_key_pool()
    assert len(pool) == 4
    assert pool[0]["provider"] == "deepseek" and pool[0]["model"] == "deepseek-v4-flash"
    assert pool[1]["model"] == "deepseek-v4-pro"        # 指定模型生效
    assert pool[2]["provider"] == "openrouter"
    assert pool[2]["base_url"] == "https://openrouter.ai/api/v1"
    assert pool[3]["provider"] == "volc"
    assert pool[3]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"


def test_parse_key_pool_custom_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_KEY_1", "custom|https://api.my.com/v1|my-model|sk-x")
    pool = provider.parse_key_pool()
    assert pool[0]["base_url"] == "https://api.my.com/v1"
    assert pool[0]["model"] == "my-model" and pool[0]["api_key"] == "sk-x"


def test_parse_key_pool_skips_invalid(monkeypatch):
    monkeypatch.setenv("LLM_KEY_1", "unknown_provider|sk-1")   # 未知服务商
    monkeypatch.setenv("LLM_KEY_2", "deepseek|")               # 缺 key
    monkeypatch.setenv("LLM_KEY_3", "custom|https://x|key")    # custom 段数不足
    monkeypatch.setenv("LLM_KEY_4", "deepseek|sk-good")
    pool = provider.parse_key_pool()
    assert len(pool) == 1 and pool[0]["api_key"] == "sk-good"


def test_parse_key_pool_dedupes_same_key(monkeypatch):
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-same")
    monkeypatch.setenv("LLM_KEY_2", "openrouter|sk-same")      # 同 key 不同服务商
    assert len(provider.parse_key_pool()) == 1


def test_key_pool_mode_detection(mixed_pool, no_pool, monkeypatch):
    """key 池有配置 → True；清空后 → False（no_pool fixture 先执行清理）"""
    assert provider.key_pool_mode() is False  # no_pool 已清空全部 LLM_KEY_*
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-x")
    assert provider.key_pool_mode() is True


def test_profiles_info_key_pool(mixed_pool):
    info = provider.profiles_info()
    assert info["mode"] == "key_pool"
    assert len(info["key_pool"]) == 4
    assert all("api_key" not in e for e in info["key_pool"])  # 不泄露 key
    assert info["key_pool"][2]["provider"] == "openrouter"
    assert "deepseek" in info["providers"] and "volc" in info["providers"]


# ---------------- 跨服务商轮换 ----------------

def test_chat_cross_provider_switch_on_402(mixed_pool, monkeypatch):
    """deepseek 两个 key 都余额不足 → 无缝跨到 openrouter 成功"""
    calls = []

    def fake(base_url, model, api_key, messages, temperature):
        calls.append((base_url, api_key))
        if "deepseek.com" in base_url:
            raise _http_err(402)
        return _ok(model)

    monkeypatch.setattr(provider, "_chat_once", fake)
    result = provider.chat(None, [{"role": "user", "content": "hi"}])
    assert result["profile"] == "openrouter"
    assert len(calls) == 3  # ds#1 → ds#2 → openrouter


def test_chat_pool_switch_on_timeout_and_limit(mixed_pool, monkeypatch):
    """超时(deepseek) + 限流(openrouter) → 火山方舟成功"""
    calls = []

    def fake(base_url, model, api_key, messages, temperature):
        calls.append(base_url)
        if "deepseek.com" in base_url:
            raise TimeoutError("connect timeout")
        if "openrouter" in base_url:
            raise _http_err(429)
        return _ok(model)

    monkeypatch.setattr(provider, "_chat_once", fake)
    result = provider.chat(None, [{"role": "user", "content": "hi"}])
    assert result["profile"] == "volc" and len(calls) == 4  # ds×2 → or → volc


def test_chat_pool_filter_by_provider(mixed_pool, monkeypatch):
    """profile 指定服务商：只用该服务商的条目（其余跳过）"""
    calls = []

    def fake(base_url, model, api_key, messages, temperature):
        calls.append(api_key)
        if api_key == "sk-ds-1":
            raise _http_err(402)
        return _ok(model)

    monkeypatch.setattr(provider, "_chat_once", fake)
    result = provider.chat("deepseek", [{"role": "user", "content": "hi"}])
    assert result["profile"] == "deepseek" and result["model"] == "deepseek-v4-pro"
    assert calls == ["sk-ds-1", "sk-ds-2"]  # 只试了 deepseek 的两个 key


def test_chat_pool_unknown_provider(mixed_pool):
    with pytest.raises(provider.LLMError, match="没有服务商"):
        provider.chat("not_exist", [{"role": "user", "content": "hi"}])


def test_chat_pool_all_exhausted(mixed_pool, monkeypatch):
    monkeypatch.setattr(provider, "_chat_once",
                        lambda *a, **k: (_ for _ in ()).throw(_http_err(402)))
    with pytest.raises(provider.LLMError, match="Key 池全部条目不可用"):
        provider.chat(None, [{"role": "user", "content": "hi"}])


# ---------------- 无配置回退 ----------------

def test_chat_no_config_friendly_error(no_pool):
    """未配置任何 key → 友好提示"""
    with pytest.raises(provider.LLMError, match="未配置"):
        provider.chat(None, [{"role": "user", "content": "hi"}])


def test_chat_fallback_to_profiles_when_pool_empty(monkeypatch):
    """key 池为空但旧 profiles 变量存在 → 回退 profiles 模式（兼容）"""
    monkeypatch.delenv("LLM_KEY", raising=False)
    for i in range(2, 10):
        monkeypatch.delenv(f"LLM_KEY_{i}", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setattr(provider, "_chat_once",
                        lambda base_url, model, api_key, m, t: _ok(model))
    result = provider.chat(None, [{"role": "user", "content": "hi"}])
    assert result["model"] == "deepseek-v4-flash"  # 内置默认 main profile


# ---------------- DB Key 池（用户私有，最高优先级） ----------------

@pytest.fixture()
def db_pool(monkeypatch, tmp_path):
    """模拟用户 DB 中配了 2 条异构 key（mock db.list_llm_keys）"""
    monkeypatch.delenv("LLM_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY_1", raising=False)
    for i in range(2, 10):
        monkeypatch.delenv(f"LLM_KEY_{i}", raising=False)
    fake_keys = [
        {"id": 11, "username": "u", "provider": "deepseek", "model": None, "base_url": None,
         "api_key": "sk-db-1", "label": "DB1", "sort_order": 1, "enabled": True},
        {"id": 12, "username": "u", "provider": "openrouter", "model": "deepseek/deepseek-chat",
         "base_url": None, "api_key": "sk-db-2", "label": "DB2", "sort_order": 2,
         "enabled": True},
        {"id": 13, "username": "u", "provider": "volc", "model": None, "base_url": None,
         "api_key": "sk-db-3", "label": "禁用", "sort_order": 3, "enabled": False},  # 禁用条目
    ]

    def fake_list(username, db_path=None):
        return [k for k in fake_keys if k["enabled"]]

    import app.llm.provider as prov
    monkeypatch.setattr(prov.db, "list_llm_keys", fake_list)
    return fake_keys


def test_chat_db_pool_priority_and_rotation(db_pool, monkeypatch):
    """用户 DB 池优先于环境变量池；DB 第一条 402 → 跨到 DB 第二条"""
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-env-should-not-be-used")
    calls = []

    def fake(base_url, model, api_key, messages, temperature):
        calls.append(api_key)
        if api_key == "sk-db-1":
            raise _http_err(402)
        return _ok(model)

    monkeypatch.setattr(provider, "_chat_once", fake)
    result = provider.chat(None, [{"role": "user", "content": "hi"}], username="u")
    assert result["profile"] == "openrouter"
    assert calls == ["sk-db-1", "sk-db-2"]  # env key 未被使用；禁用条目被跳过


def test_chat_db_pool_disabled_entries_skipped(db_pool, monkeypatch):
    monkeypatch.setattr(provider, "_chat_once",
                        lambda b, m, k, ms, t: (_ for _ in ()).throw(AssertionError("禁用条目不应被调用"))
                        if k == "sk-db-3" else _ok(m))
    result = provider.chat(None, [{"role": "user", "content": "hi"}], username="u")
    assert result["profile"] == "deepseek"


def test_chat_db_pool_by_key_id(db_pool, monkeypatch):
    """profile 传 key_id（数字）→ 只用该条"""
    calls = []

    def fake(base_url, model, api_key, messages, temperature):
        calls.append(api_key)
        return _ok(model)

    monkeypatch.setattr(provider, "_chat_once", fake)
    result = provider.chat("12", [{"role": "user", "content": "hi"}], username="u")
    assert result["profile"] == "openrouter" and calls == ["sk-db-2"]


def test_chat_db_pool_empty_falls_to_env(monkeypatch):
    """用户无 DB key → 回退环境变量池"""
    import app.llm.provider as prov
    monkeypatch.delenv("LLM_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY_1", raising=False)
    for i in range(2, 10):
        monkeypatch.delenv(f"LLM_KEY_{i}", raising=False)
    monkeypatch.setenv("LLM_KEY_1", "deepseek|sk-env-1")
    monkeypatch.setattr(prov.db, "list_llm_keys", lambda u, db_path=None: [])
    monkeypatch.setattr(provider, "_chat_once", lambda b, m, k, ms, t: _ok(m))
    result = provider.chat(None, [{"role": "user", "content": "hi"}], username="nobody")
    assert result["profile"] == "deepseek"
