# -*- coding: utf-8 -*-
"""策略列表"""
from fastapi import APIRouter, Depends

from ..auth import get_current_user
from ..engine.strategies import REGISTRY

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("")
def list_strategies(_user: str = Depends(get_current_user)):
    out = []
    for s in REGISTRY.values():
        out.append({
            "id": s.id, "name": s.name, "description": s.description,
            "periods": s.periods, "param_schema": s.param_schema,
        })
    return out
