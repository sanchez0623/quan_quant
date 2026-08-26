# -*- coding: utf-8 -*-
"""LLM Provider 层：统一 Key 池（跨服务商无缝切换）+ profiles 兼容回退

两种配置模式（自动选择）：
1. Key 池模式（推荐）：.env 中配置 LLM_KEY_1 ~ LLM_KEY_9，一行一个 key，顺序即优先级。
   支持 DeepSeek / OpenRouter / 火山方舟 / 智谱 / 硅基流动 / Ollama 等异构服务商，
   一个 key 没额度(402)/失效(401)/限流(429)或调用失败 → 自动切换下一个（跨服务商）。
   格式（|分隔，key 永远是最后一段）：
     LLM_KEY_1=deepseek|sk-xxx                          # 服务商|key（用内置默认模型）
     LLM_KEY_2=openrouter|sk-or-yyy|deepseek/deepseek-chat   # 服务商|模型|key
     LLM_KEY_3=custom|https://api.xxx.com/v1|my-model|sk-zzz # 全自定义（任意OpenAI兼容端点）
2. profiles 模式（兼容）：key 池为空时回退 config/llm.yaml 的 profiles + fallback_chain，
   支持 api_key_env 主变量 + {主变量}_2~_9 同服务商多 key 轮换。
"""
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from .. import config, db

# ---------------- 内置服务商注册表（OpenAI 兼容协议） ----------------
# key 池模式下免 yaml 配置；火山方舟模型可用接入点ID(ep-xxx)或方舟模型名
PROVIDER_REGISTRY: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
        "label": "DeepSeek",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-chat",
        "label": "OpenRouter",
    },
    "volc": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-1.6",
        "label": "火山方舟",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "label": "智谱GLM",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "label": "硅基流动",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "default_model": "qwen2.5:7b",
        "label": "Ollama本地",
    },
}

# profiles 模式的内置默认配置（config/llm.yaml 与 config.example/llm.yaml 都不存在时使用）
DEFAULT_LLM_CONFIG = {
    "profiles": {
        "main": {
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "zhipu": {
            "provider": "openai_compatible",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4-flash",
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
    "fallback_chain": ["main", "zhipu", "cheap"],
}

_config_cache: Optional[dict] = None


class LLMError(RuntimeError):
    pass


# ================= Key 池模式 =================

def parse_key_pool() -> list[dict]:
    """解析 .env 中的 LLM_KEY_1 ~ LLM_KEY_9（LLM_KEY 视作 1 号）。
    返回条目列表：{index, provider, base_url, model, api_key}；格式非法的跳过并告警。"""
    entries: list[dict] = []
    seen_keys: set[str] = set()
    # 1 号条目兼容 LLM_KEY 与 LLM_KEY_1 两种写法（均视为最高优先级）
    names = ["LLM_KEY_1", "LLM_KEY"] + [f"LLM_KEY_{i}" for i in range(2, 10)]
    for env_name in names:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        provider = parts[0].lower()
        api_key = parts[-1]
        if not api_key:
            print(f"[llm] {env_name} 缺少 API Key，已跳过", file=sys.stderr)
            continue
        if provider == "custom":
            # custom|base_url|model|key
            if len(parts) < 4:
                print(f"[llm] {env_name} custom 格式需 custom|base_url|model|key，已跳过", file=sys.stderr)
                continue
            base_url, model = parts[1], parts[2]
            label = "自定义"
        elif provider in PROVIDER_REGISTRY:
            reg = PROVIDER_REGISTRY[provider]
            base_url = reg["base_url"]
            model = parts[1] if len(parts) == 3 else reg["default_model"]
            label = reg["label"]
        else:
            print(f"[llm] {env_name} 未知服务商 '{provider}'"
                  f"（可选：{'/'.join(PROVIDER_REGISTRY)} 或 custom），已跳过", file=sys.stderr)
            continue
        if api_key in seen_keys:  # 同一 key 重复配置去重
            continue
        seen_keys.add(api_key)
        entries.append({"index": len(entries) + 1, "provider": provider, "label": label,
                        "base_url": base_url, "model": model, "api_key": api_key})
    return entries


def key_pool_mode() -> bool:
    """Key 池模式 = 至少配置了一个合法 LLM_KEY_N 条目"""
    return bool(parse_key_pool())


# ================= profiles 模式（兼容旧配置） =================

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
    """profile 的全部 API Key：主变量 + api_key_envs 列表 + {主变量}_2~_9 自动发现（去重）"""
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
    if main_env:
        for i in range(2, 10):
            _add(f"{main_env}_{i}")
    return keys


def profile_api_key(profile: dict) -> Optional[str]:
    keys = profile_api_keys(profile)
    return keys[0] if keys else None


# ================= 状态信息（profiles 接口） =================

def db_key_entries(username: str, db_path: Optional[str] = None) -> list[dict]:
    """把用户 DB key 池转成调用条目（enabled only，按 sort_order）"""
    from .. import db as _db
    entries = []
    for k in _db.list_llm_keys(username, db_path):
        if not k["enabled"]:
            continue
        provider = k["provider"]
        if provider == "custom":
            base_url, model = k["base_url"] or "", k["model"] or ""
            if not base_url or not model:
                continue  # custom 条目信息不全，跳过
            label = "自定义"
        else:
            reg = PROVIDER_REGISTRY.get(provider)
            if reg is None:
                continue  # 未知服务商（注册表演进后旧数据），跳过
            base_url = reg["base_url"]
            model = k["model"] or reg["default_model"]
            label = reg["label"]
        entries.append({"index": len(entries) + 1, "provider": provider, "label": label,
                        "base_url": base_url, "model": model, "api_key": k["api_key"],
                        "key_id": k["id"], "key_label": k["label"]})
    return entries


def profiles_info(username: Optional[str] = None, db_path: Optional[str] = None) -> dict:
    """用户视角的 LLM 状态：DB key 池（私有）优先，环境变量池（系统级兜底）次之"""
    user_pool = db_key_entries(username, db_path) if username else []
    env_pool = parse_key_pool()
    out: dict = {
        "mode": "db_key_pool" if user_pool else ("key_pool" if env_pool else "profiles"),
        "user_key_pool": [{k: e[k] for k in
                           ("index", "provider", "label", "base_url", "model", "key_id", "key_label")}
                          for e in user_pool],  # 不返回 api_key 本身
        "key_pool": [{k: e[k] for k in ("index", "provider", "label", "base_url", "model")}
                     for e in env_pool],
        "providers": list(PROVIDER_REGISTRY),
    }
    cfg = load_llm_config()
    out["profiles"] = [{
        "name": name,
        "provider": p.get("provider", "openai_compatible"),
        "base_url": p.get("base_url", ""),
        "model": p.get("model", ""),
        "api_key_env": p.get("api_key_env", ""),
        "keys": len(profile_api_keys(p)),
        "available": bool(profile_api_keys(p)),
    } for name, p in (cfg.get("profiles") or {}).items()]
    out["default"] = cfg.get("default", "main")
    out["fallback_chain"] = cfg.get("fallback_chain", [])
    return out


# ================= 调用 =================

def _chat_once(base_url: str, model: str, api_key: str, messages: list,
               temperature: float, timeout: float = 120.0) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": messages, "temperature": temperature}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.time()
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.post(url, json=body, headers=headers)
    elapsed = round(time.time() - t0, 3)
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise LLMError("LLM 返回空回复")
    usage = data.get("usage") or {}
    pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    return {"content": content, "model": model, "prompt_tokens": pt,
            "completion_tokens": ct, "elapsed": elapsed}


def _record_usage(profile_name: str, result: dict, db_path: Optional[str]) -> None:
    try:
        db.record_llm_usage(profile_name, result["model"], result["prompt_tokens"],
                            result["completion_tokens"], result["elapsed"], db_path)
    except Exception:  # noqa: BLE001
        pass


def _chat_via_pool(profile_name: Optional[str], messages: list,
                   temperature: float, db_path: Optional[str],
                   pool: Optional[list[dict]] = None) -> dict:
    """Key 池轮换：任何条目失败（余额/失效/限流/超时等）→ 切下一个（跨服务商）。
    profile_name 语义：None/'auto'=全池轮换；'deepseek'等服务商名=只用该服务商条目；
    数字字符串=只用该 key_id 条目。"""
    pool = pool if pool is not None else parse_key_pool()
    if profile_name and profile_name not in (None, "auto"):
        if str(profile_name).isdigit():  # 指定 key_id
            filtered = [e for e in pool if e.get("key_id") == int(profile_name)]
            if not filtered:
                raise LLMError(f"Key 池中没有 id 为 {profile_name} 的条目")
        else:  # 服务商过滤（同服务商可配多条目，仍按序轮换）
            filtered = [e for e in pool if e["provider"] == profile_name]
            if not filtered:
                raise LLMError(f"Key 池中没有服务商 '{profile_name}' 的条目")
        pool = filtered
    last_err = None
    for entry in pool:
        try:
            result = _chat_once(entry["base_url"], entry["model"], entry["api_key"],
                                messages, temperature)
            _record_usage(entry["provider"], result, db_path)
            return {"content": result["content"], "model": result["model"],
                    "tokens": result["prompt_tokens"] + result["completion_tokens"],
                    "elapsed": result["elapsed"], "profile": entry["provider"]}
        except Exception as exc:  # noqa: BLE001  任何失败（401/402/429/超时/5xx）→ 切下一个
            last_err = f"[{entry['provider']}#{entry['index']}] {exc}"
            print(f"[llm] key#{entry['index']}({entry['provider']}) 调用失败，"
                  f"切换下一个: {exc}", file=sys.stderr)
    raise LLMError(f"Key 池全部条目不可用（最后错误: {last_err}）")


def _chat_via_profiles(profile_name: Optional[str], messages: list,
                       temperature: float, db_path: Optional[str]) -> dict:
    """profiles 兼容模式：指定 profile 的 key 轮换 → fallback_chain 降级"""
    cfg = load_llm_config()
    profiles = cfg.get("profiles") or {}
    if not profiles:
        raise LLMError("未配置 LLM API Key，请在 .env 中配置 LLM_KEY_1（推荐）或旧 profiles 变量")
    chain = [profile_name] if profile_name and profile_name in profiles else []
    chain += [c for c in (cfg.get("fallback_chain") or list(profiles)) if c not in chain]
    configured = False
    last_err = None
    for name in chain:
        p = profiles.get(name)
        if not p:
            continue
        keys = profile_api_keys(p)
        if not keys:
            continue
        configured = True
        for ki, key in enumerate(keys, start=1):
            try:
                result = _chat_once(p["base_url"], p["model"], key, messages, temperature)
                _record_usage(name, result, db_path)
                return {"content": result["content"], "model": result["model"],
                        "tokens": result["prompt_tokens"] + result["completion_tokens"],
                        "elapsed": result["elapsed"], "profile": name}
            except Exception as e:  # noqa: BLE001
                last_err = f"profile {name} key#{ki} 调用失败: {e}"
                import httpx as _hx
                key_level = (isinstance(e, _hx.HTTPStatusError)
                             and e.response.status_code in (401, 402, 429))
                if key_level and ki < len(keys):
                    print(f"[llm] profile {name} key#{ki} 不可用，切换 key#{ki + 1}",
                          file=sys.stderr)
                    continue
                break  # 非 key 级错误或 key 用尽 → 换 profile
    if not configured:
        raise LLMError("未配置 LLM API Key，请在 .env 中配置 LLM_KEY_1（推荐）或旧 profiles 变量")
    raise LLMError(f"所有 LLM profile 均不可用（最后错误: {last_err}）")


def chat(profile_name: Optional[str], messages: list, temperature: float = 0.3,
         db_path: Optional[str] = None, username: Optional[str] = None) -> dict:
    """统一入口。三级 Key 池（跨服务商无缝切换）：
    1. 用户 DB Key 池（私有，前端增删改管理）
    2. 环境变量池 LLM_KEY_1~9（系统级公共兜底）
    3. profiles 配置（llm.yaml 兼容）
    返回 {content, model, tokens, elapsed, profile}。"""
    if username:
        user_pool = db_key_entries(username, db_path)
        if user_pool:
            return _chat_via_pool(profile_name, messages, temperature, db_path, pool=user_pool)
    if key_pool_mode():
        return _chat_via_pool(profile_name, messages, temperature, db_path)
    return _chat_via_profiles(profile_name, messages, temperature, db_path)
