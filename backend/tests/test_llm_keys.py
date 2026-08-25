# -*- coding: utf-8 -*-
"""LLM 多 API Key 无缝切换单元测试（mock 网络调用）"""
import httpx
import pytest

from app.llm import provider


@pytest.fixture()
def deepseek_keys(monkeypatch):
    """模拟 .env 配置 3 个 key"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key-A")
    monkeypatch.setenv("DEEPSEEK_API_KEY_2", "key-B")
    monkeypatch.setenv("DEEPSEEK_API_KEY_3", "key-C")
    monkeypatch.delenv("DEEPSEEK_API_KEY_4", raising=False)


def _http_err(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


# ---------------- key 发现 ----------------

def test_profile_api_keys_collect_and_dedupe(deepseek_keys):
    profile = {"api_key_env": "DEEPSEEK_API_KEY"}
    assert provider.profile_api_keys(profile) == ["key-A", "key-B", "key-C"]


def test_profile_api_keys_dedupe_same_value(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "same")
    monkeypatch.setenv("DEEPSEEK_API_KEY_2", "same")
    assert provider.profile_api_keys({"api_key_env": "DEEPSEEK_API_KEY"}) == ["same"]


def test_profile_api_keys_explicit_envs(monkeypatch):
    monkeypatch.setenv("MY_KEY_X", "x")
    monkeypatch.setenv("MY_KEY_Y", "y")
    profile = {"api_key_envs": ["MY_KEY_X", "MY_KEY_Y"]}
    assert provider.profile_api_keys(profile) == ["x", "y"]


def test_profile_available_any_key(deepseek_keys):
    assert provider.profile_available("main") is True


def test_profiles_info_key_count(deepseek_keys):
    info = provider.profiles_info()
    main = [p for p in info["profiles"] if p["name"] == "main"][0]
    assert main["keys"] == 3 and main["available"] is True


# ---------------- key 轮换 ----------------

def test_chat_switches_key_on_402(deepseek_keys, monkeypatch):
    """key-A 余额不足(402) → 无缝切换 key-B 成功"""
    calls = []

    def fake_chat_once(profile, api_key, messages, temperature, db_path):
        calls.append(api_key)
        if api_key == "key-A":
            raise _http_err(402)  # 余额不足
        return {"content": "ok", "model": profile["model"],
                "prompt_tokens": 10, "completion_tokens": 5, "elapsed": 0.1}

    monkeypatch.setattr(provider, "_chat_once", fake_chat_once)
    result = provider.chat("main", [{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert calls == ["key-A", "key-B"]  # 只试了两个 key，第三个未用


def test_chat_switches_key_on_401_and_429(deepseek_keys, monkeypatch):
    """key-A 失效(401)、key-B 限流(429) → key-C 成功"""
    calls = []

    def fake_chat_once(profile, api_key, messages, temperature, db_path):
        calls.append(api_key)
        if api_key == "key-A":
            raise _http_err(401)
        if api_key == "key-B":
            raise _http_err(429)
        return {"content": "ok", "model": profile["model"],
                "prompt_tokens": 1, "completion_tokens": 1, "elapsed": 0.1}

    monkeypatch.setattr(provider, "_chat_once", fake_chat_once)
    result = provider.chat("main", [{"role": "user", "content": "hi"}])
    assert result["content"] == "ok" and calls == ["key-A", "key-B", "key-C"]


def test_chat_non_key_error_skips_to_next_profile(deepseek_keys, monkeypatch):
    """超时等非 key 级错误：不烧其余 key，直接降级到下一个 profile"""
    calls = []

    def fake_chat_once(profile, api_key, messages, temperature, db_path):
        calls.append((profile["model"], api_key))
        if profile["model"] == "deepseek-v4-flash":
            raise TimeoutError("connect timeout")
        return {"content": "ok-pro", "model": profile["model"],
                "prompt_tokens": 1, "completion_tokens": 1, "elapsed": 0.1}

    monkeypatch.setattr(provider, "_chat_once", fake_chat_once)
    result = provider.chat("main", [{"role": "user", "content": "hi"}])
    assert result["content"] == "ok-pro"
    assert calls == [("deepseek-v4-flash", "key-A"), ("deepseek-v4-pro", "key-A")]


def test_chat_all_keys_exhausted_raises(deepseek_keys, monkeypatch):
    """所有 key 均余额不足 → 报错含明确信息"""
    def fake_chat_once(profile, api_key, messages, temperature, db_path):
        raise _http_err(402)

    monkeypatch.setattr(provider, "_chat_once", fake_chat_once)
    with pytest.raises(provider.LLMError, match="不可用"):
        provider.chat("main", [{"role": "user", "content": "hi"}])


def test_chat_no_key_friendly_error(monkeypatch):
    """未配置任何 key → 友好提示"""
    for i in ("", "_2", "_3", "_4"):
        monkeypatch.delenv(f"DEEPSEEK_API_KEY{i}", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(provider.LLMError, match="未配置"):
        provider.chat(None, [{"role": "user", "content": "hi"}])
