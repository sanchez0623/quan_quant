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


@router.get("/by-codes")
def stocks_by_codes(codes: str = Query(default=""),
                    _user: str = Depends(get_current_user)):
    """按代码批量返回 {code, name, st}（按输入顺序）。
    支持逗号/空格/换行分隔，兼容 sh.600000 / 600000.SH 前缀写法。"""
    basic = store.read_stock_basic()
    if basic is None or basic.height == 0 or not codes:
        return []
    wanted: list[str] = []
    for raw in codes.replace(",", " ").replace("，", " ").replace("\n", " ").split():
        raw = raw.strip().lower()
        if not raw:
            continue
        if "." in raw:  # sh.600000 / 600000.SH -> 600000
            head, tail = raw.split(".", 1)
            raw = head if head.isdigit() else tail
        if raw.isdigit() and raw not in wanted:
            wanted.append(raw)
    if not wanted:
        return []
    df = basic.filter(pl.col("code").is_in(wanted))
    rows = df.select(["code", "name", "st"]).to_dicts()
    order = {c: i for i, c in enumerate(wanted)}
    rows.sort(key=lambda r: order.get(r["code"], 10**9))
    return [{"code": r["code"], "name": r["name"], "st": bool(r["st"])} for r in rows]
