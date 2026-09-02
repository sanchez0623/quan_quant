# -*- coding: utf-8 -*-
"""LLM Key 管理接口：每用户私有的 Key 池（增删改查 + 连通性测试）"""
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..llm.provider import PROVIDER_REGISTRY, _chat_once

router = APIRouter(prefix="/api/keys", tags=["keys"])

PROVIDERS = list(PROVIDER_REGISTRY) + ["custom"]


@router.get("")
def list_keys(user: str = Depends(get_current_user)):
    """当前用户的 key 列表（脱敏）+ 内置服务商注册表（供前端下拉与默认模型）"""
    return {"keys": db.llm_keys_masked(user), "providers": PROVIDERS,
            "registry": {name: {"base_url": reg["base_url"],
                                "default_model": reg["default_model"],
                                "label": reg["label"]}
                         for name, reg in PROVIDER_REGISTRY.items()}}


class KeyCreate(BaseModel):
    provider: str
    api_key: str = Field(min_length=8)
    model: Optional[str] = None
    base_url: Optional[str] = None
    label: str = ""
    sort_order: int = 0
    timeout: Optional[float] = Field(default=None, gt=0)
    max_tokens: Optional[int] = Field(default=None, gt=0)


@router.post("")
def add_key(req: KeyCreate, user: str = Depends(get_current_user)):
    provider = req.provider.lower().strip()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"provider 需为 {PROVIDERS} 之一")
    if provider == "custom" and not (req.base_url and req.model):
        raise HTTPException(status_code=400, detail="custom 服务商需填写 base_url 与 model")
    if provider != "custom" and req.base_url:
        raise HTTPException(status_code=400, detail="内置服务商无需填写 base_url")
    key_id = db.add_llm_key(user, provider, req.api_key.strip(), model=req.model or None,
                            base_url=req.base_url or None, label=req.label,
                            sort_order=req.sort_order,
                            timeout=req.timeout, max_tokens=req.max_tokens)
    # 优先级归一化：新 key 放到指定位置（1-based，<=0 视为末位），其余顺延
    n = len(db.list_llm_keys(user))
    target = req.sort_order if req.sort_order and req.sort_order > 0 else n
    _renumber_keys(user, key_id, target)
    return {"id": key_id, "status": "ok"}


class KeyUpdate(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = Field(default=None, min_length=8)
    model: Optional[str] = None
    base_url: Optional[str] = None
    label: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None
    timeout: Optional[float] = Field(default=None, gt=0)
    max_tokens: Optional[int] = Field(default=None, gt=0)


@router.put("/{key_id}")
def update_key(key_id: int, req: KeyUpdate, user: str = Depends(get_current_user)):
    fields: dict = {}
    if req.provider is not None:
        provider = req.provider.lower().strip()
        if provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"provider 需为 {PROVIDERS} 之一")
        fields["provider"] = provider
    if req.api_key is not None:
        fields["api_key"] = req.api_key.strip()
    if req.model is not None:
        fields["model"] = req.model
    if req.base_url is not None:
        fields["base_url"] = req.base_url
    if req.label is not None:
        fields["label"] = req.label
    if req.sort_order is not None:
        fields["sort_order"] = int(req.sort_order)
    if req.enabled is not None:
        fields["enabled"] = 1 if req.enabled else 0
    if req.timeout is not None:
        fields["timeout"] = req.timeout
    if req.max_tokens is not None:
        fields["max_tokens"] = int(req.max_tokens)
    if not db.update_llm_key(key_id, user, **fields):
        raise HTTPException(status_code=404, detail="Key 不存在或不属于当前用户")
    # 改了优先级：移动到指定位置（1-based），其余整体顺延（重排为唯一 1..N）
    if req.sort_order is not None:
        _renumber_keys(user, key_id, req.sort_order)
    return {"status": "ok"}


@router.delete("/{key_id}")
def remove_key(key_id: int, user: str = Depends(get_current_user)):
    if not db.delete_llm_key(key_id, user):
        raise HTTPException(status_code=404, detail="Key 不存在或不属于当前用户")
    return {"status": "ok"}


def _renumber_keys(user: str, moved_id: int, target_pos: int) -> None:
    """优先级归一化：把 key(moved_id) 移到第 target_pos 位（1-based），
    其余按当前顺序顺延，最终所有 key 的 sort_order 重排为唯一 1..N。

    语义：优先级 = 位置（1=最先用）；越界自动钳制到首/末位。
    同时把历史任意权重（如 10/20/30）一次归一化。"""
    keys = db.list_llm_keys(user)
    moved = next((k for k in keys if k["id"] == moved_id), None)
    if moved is None:
        return
    others = [k for k in keys if k["id"] != moved_id]
    others.sort(key=lambda k: (k["sort_order"], k["id"]))
    pos = max(1, min(int(target_pos), len(keys)))
    ordered = others[: pos - 1] + [moved] + others[pos - 1:]
    with db.conn() as c:
        for i, k in enumerate(ordered, start=1):
            c.execute("UPDATE llm_keys SET sort_order=? WHERE id=?", (i, k["id"]))


def _http_error_detail(e: httpx.HTTPStatusError) -> str:
    """把服务商 HTTP 错误转成可读原因（含服务商返回的原始 message）"""
    code = e.response.status_code
    reason = {
        401: "API Key 无效或已过期",
        402: "余额不足或欠费",
        403: "无权限访问",
        404: "端点或模型不存在（请检查 base_url / 模型名）",
        429: "触发限流",
    }.get(code, f"HTTP {code}")
    try:
        body = e.response.json()
        msg = (body.get("error") or {}).get("message") or body.get("message") or ""
        if msg:
            return f"{reason}：{str(msg)[:200]}"
    except Exception:  # noqa: BLE001
        pass
    return reason


@router.post("/{key_id}/test")
def test_key(key_id: int, user: str = Depends(get_current_user)):
    """连通性测试：用该 Key 向对应服务商发送「你好」，确认能联通。"""
    rec = next((k for k in db.list_llm_keys(user) if k["id"] == key_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="Key 不存在或不属于当前用户")
    if not rec["enabled"]:
        raise HTTPException(status_code=400, detail="该 Key 已禁用，请先启用再测试")
    if rec["provider"] == "custom":
        if not rec["base_url"] or not rec["model"]:
            raise HTTPException(status_code=400, detail="自定义服务商需填写 base_url 与模型名")
        base_url, model = rec["base_url"], rec["model"]
    else:
        reg = PROVIDER_REGISTRY.get(rec["provider"])
        if reg is None:
            raise HTTPException(status_code=400, detail=f"未知服务商 {rec['provider']}，无法测试")
        base_url, model = reg["base_url"], rec["model"] or reg["default_model"]
    try:
        result = _chat_once(base_url, model, rec["api_key"],
                            [{"role": "user", "content": "你好"}],
                            temperature=0.3, timeout=20.0)
        return {"ok": True, "reply": (result["content"] or "").strip()[:200],
                "model": result["model"], "elapsed": result["elapsed"]}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"连接失败：{_http_error_detail(e)}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=400, detail="连接超时（20秒内无响应），请检查网络或服务商状态")
    except Exception as e:  # noqa: BLE001  连接/解析错误等
        raise HTTPException(status_code=400, detail=f"连接失败：{e}")
