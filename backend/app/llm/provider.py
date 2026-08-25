# -*- coding: utf-8 -*-
"""OpenAI 兼容多 Provider：llm.yaml profiles、fallback chain、用量记录（trust_env=False）"""
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from .. import config, db

# 内置默认配置（config/llm.yaml 与 config.example/llm.yaml 都不存在时使用）
DEFAULT_LLM_CONFIG = {
    "profiles": {
        "main": {            # 默认分析模型：DeepSeek V4 Flash（快且便宜）
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "deepseek_pro": {    # 深度分析可选：DeepSeek V4 Pro
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "zhipu": {
            "provider": "openai_compatible",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4-flash",
            "api_key_env": "ZHIPU_API_KEY",
        },
        "cheap": {           # 轻量任务：Ollama 本地零成本
            "provider": "openai_compatible",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5:7b",
            "api_key_env": "OLLAMA_API_KEY",
        },
    },
    "default": "main",
    "fallback_chain": ["main", "deepseek_pro", "zhipu", "cheap"],
}

_config_cache: Optional[dict] = None


class LLMError(RuntimeError):
    pass


def load_llm_config() -> dict:
    """优先 config/llm.yaml → config.example/llm.yaml → 内置默认"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    for p in (config.CONFIG_DIR / "llm.yaml",
              config.CONFIG_DIR.parent / "config.example" / "llm.yaml",
              Path(config.PROJECT_ROOT) / "config.example" / "llm.yaml"):
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if data and isinstance(data.get("profiles"), dict) and data["profiles"]:
                    _config_cache = data
                    return _config_cache
            except yaml.YAMLError:
                continue
    _config_cache = DEFAULT_LLM_CONFIG
    return _config_cache


def reload_config() -> dict:
    global _config_cache
    _config_cache = None
    return load_llm_config()


def profile_api_keys(profile: dict) -> list[str]:
    """收集 profile 的全部可用 API Key（保序去重）：
    1. api_key_env 主变量
    2. api_key_envs 显式列表（yaml 声明）
    3. 自动发现 {主变量}_2 ~ {主变量}_9（.env 加 DEEPSEEK_API_KEY_2=xx 即生效，无需改 yaml）
    一个 key 没额度/限流时自动切换下一个。"""
    keys: list[str] = []

    def _add(env_name: str) -> None:
        if not env_name:
            return
        v = os.environ.get(env_name, "").strip()
        if v and v not in keys:
            keys.append(v)

    _add(profile.get("api_key_env", ""))
    for env_name in profile.get("api_key_envs") or []:
        _add(env_name)
    main_env = profile.get("api_key_env", "")
    if main_env:  # 自动发现序号后缀变量
        for i in range(2, 10):
            _add(f"{main_env}_{i}")
    return keys


def profile_api_key(profile: dict) -> Optional[str]:
    keys = profile_api_keys(profile)
    return keys[0] if keys else None


def profile_available(name: str) -> bool:
    """available = 至少一个 API Key 已配置"""
    cfg = load_llm_config()
    p = cfg.get("profiles", {}).get(name)
    if not p:
        return False
    return bool(profile_api_keys(p))


def profiles_info() -> dict:
    cfg = load_llm_config()
    out = []
    for name, p in (cfg.get("profiles") or {}).items():
        out.append({
            "name": name,
            "provider": p.get("provider", "openai_compatible"),
            "base_url": p.get("base_url", ""),
            "model": p.get("model", ""),
            "api_key_env": p.get("api_key_env", ""),
            "keys": len(profile_api_keys(p)),  # 已配置 key 数量
            "available": profile_available(name),
        })
    return {"profiles": out, "default": cfg.get("default", "main"),
            "fallback_chain": cfg.get("fallback_chain", [])}


def _chat_once(profile: dict, api_key: str, messages: list, temperature: float,
               db_path: Optional[str]) -> dict:
    url = profile["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": profile["model"], "messages": messages, "temperature": temperature}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.time()
    with httpx.Client(timeout=120.0, trust_env=False) as client:
        resp = client.post(url, json=body, headers=headers)
    elapsed = round(time.time() - t0, 3)
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise LLMError("LLM 返回空回复")
    usage = data.get("usage") or {}
    pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    return {"content": content, "model": profile["model"], "prompt_tokens": pt,
            "completion_tokens": ct, "elapsed": elapsed}


def _is_key_level_error(e: Exception) -> bool:
    """key 级错误（换 key 可解决）：401 认证失败 / 402 余额不足 / 429 限流"""
    import httpx
    return (isinstance(e, httpx.HTTPStatusError)
            and e.response.status_code in (401, 402, 429))


def chat(profile_name: Optional[str], messages: list, temperature: float = 0.3,
         db_path: Optional[str] = None) -> dict:
    """调用指定 profile；同 profile 内多 API Key 轮换（一个没 token/限流自动切下一个），
    全部 key 失败再沿 fallback_chain 换 profile；记录用量。返回
    {content, model, tokens, elapsed, profile}"""
    cfg = load_llm_config()
    profiles = cfg.get("profiles") or {}
    if not profiles:
        raise LLMError("未配置 LLM API Key，请设置环境变量或在 .env 中配置")
    chain = [profile_name] if profile_name else []
    chain += [c for c in (cfg.get("fallback_chain") or list(profiles)) if c != profile_name]
    no_key_profiles: list[str] = []
    last_err = None
    for name in chain:
        p = profiles.get(name)
        if not p:
            continue
        keys = profile_api_keys(p)
        if not keys:
            no_key_profiles.append(name)
            continue
        for ki, key in enumerate(keys, start=1):
            try:
                result = _chat_once(p, key, messages, temperature, db_path)
                try:
                    db.record_llm_usage(name, result["model"], result["prompt_tokens"],
                                        result["completion_tokens"], result["elapsed"], db_path)
                except Exception:  # noqa: BLE001
                    pass
                return {"content": result["content"], "model": result["model"],
                        "tokens": result["prompt_tokens"] + result["completion_tokens"],
                        "elapsed": result["elapsed"], "profile": name}
            except Exception as e:  # noqa: BLE001
                last_err = f"profile {name} key#{ki} 调用失败: {e}"
                if _is_key_level_error(e) and ki < len(keys):
                    # key 级错误（401/402/429）：无缝切换下一个 key
                    print(f"[llm] profile {name} key#{ki} 不可用（{e.response.status_code}），"
                          f"切换 key#{ki + 1}", file=sys.stderr)
                    continue
                break  # 非 key 级错误或 key 已用尽 → 换 profile
    if no_key_profiles and last_err is None:
        raise LLMError("未配置 LLM API Key，请设置环境变量或在 .env 中配置")
    raise LLMError(f"所有 LLM profile 均不可用（最后错误: {last_err}）")
