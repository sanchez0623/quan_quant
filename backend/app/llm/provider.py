# -*- coding: utf-8 -*-
"""OpenAI 兼容多 Provider：llm.yaml profiles、fallback chain、用量记录（trust_env=False）"""
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from .. import config, db

# 内置默认配置（config/llm.yaml 与 config.example/llm.yaml 都不存在时使用）
DEFAULT_LLM_CONFIG = {
    "profiles": {
        "main": {
            "provider": "openai_compatible",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V3",
            "api_key_env": "SILICONFLOW_API_KEY",
        },
        "deepseek": {
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "zhipu": {
            "provider": "openai_compatible",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4",
            "api_key_env": "ZHIPU_API_KEY",
        },
        "cheap": {
            "provider": "openai_compatible",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5:7b",
            "api_key_env": "OLLAMA_API_KEY",
        },
    },
    "default": "main",
    "fallback_chain": ["main", "deepseek", "zhipu", "cheap"],
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


def profile_api_key(profile: dict) -> Optional[str]:
    env_name = profile.get("api_key_env", "")
    return os.environ.get(env_name) if env_name else None


def profile_available(name: str) -> bool:
    """available = 对应环境变量已配置"""
    cfg = load_llm_config()
    p = cfg.get("profiles", {}).get(name)
    if not p:
        return False
    key = profile_api_key(p)
    return bool(key)


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


def chat(profile_name: Optional[str], messages: list, temperature: float = 0.3,
         db_path: Optional[str] = None) -> dict:
    """调用指定 profile，失败沿 fallback_chain 降级；记录用量。返回
    {content, model, tokens, elapsed, profile}"""
    cfg = load_llm_config()
    profiles = cfg.get("profiles") or {}
    if not profiles:
        raise LLMError("未配置 LLM API Key，请设置环境变量或在 .env 中配置")
    chain = [profile_name] if profile_name else []
    chain += [c for c in (cfg.get("fallback_chain") or list(profiles)) if c != profile_name]
    last_err = None
    for name in chain:
        p = profiles.get(name)
        if not p:
            continue
        key = profile_api_key(p)
        if not key:
            last_err = f"profile {name} 未配置 API Key（{p.get('api_key_env')}）"
            continue
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
        except Exception as e:  # noqa: BLE001  超时/HTTP错误/空回复 → 降级
            last_err = f"profile {name} 调用失败: {e}"
            continue
    raise LLMError(f"未配置 LLM API Key，请设置环境变量或在 .env 中配置（最后错误: {last_err}）"
                   if last_err and "未配置" in str(last_err)
                   else f"所有 LLM profile 均不可用（最后错误: {last_err}）")
