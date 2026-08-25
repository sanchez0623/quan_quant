# -*- coding: utf-8 -*-
"""股票查询：本地 stock_basic.parquet 模糊匹配 code 或 name"""
from fastapi import APIRouter, Depends, Query

import polars as pl

from ..auth import get_current_user
from ..data import store

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("")
def search_stocks(keyword: str = Query(default=""), limit: int = Query(default=20, ge=1, le=100),
                  _user: str = Depends(get_current_user)):
    basic = store.read_stock_basic()
    if basic is None or basic.height == 0:
        return []
    if keyword:
        kw = keyword.strip()
        basic = basic.filter(pl.col("code").str.contains(kw, literal=True)
                             | pl.col("name").str.contains(kw, literal=True))
    rows = (basic.sort("code").head(limit).select(["code", "name", "st"]).to_dicts())
    return [{"code": r["code"], "name": r["name"], "st": bool(r["st"])} for r in rows]
