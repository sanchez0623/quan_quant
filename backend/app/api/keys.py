# -*- coding: utf-8 -*-
"""LLM Key 管理接口：每用户私有的 Key 池（增删改查）"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..llm.provider import PROVIDER_REGISTRY

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
                            sort_order=req.sort_order)
    return {"id": key_id, "status": "ok"}


class KeyUpdate(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = Field(default=None, min_length=8)
    model: Optional[str] = None
    base_url: Optional[str] = None
    label: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None


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
    if not db.update_llm_key(key_id, user, **fields):
        raise HTTPException(status_code=404, detail="Key 不存在或不属于当前用户")
    return {"status": "ok"}


@router.delete("/{key_id}")
def remove_key(key_id: int, user: str = Depends(get_current_user)):
    if not db.delete_llm_key(key_id, user):
        raise HTTPException(status_code=404, detail="Key 不存在或不属于当前用户")
    return {"status": "ok"}
