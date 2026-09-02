# -*- coding: utf-8 -*-
"""数据管理接口：状态 / 增量更新 / 演示数据"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..data import sources, store
from ..task_manager import manager

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/status")
def data_status(_user: str = Depends(get_current_user)):
    return {
        "daily": store.parquet_stats_daily(),
        "minute5": store.parquet_stats_minute5(),
        "adj_factor": store.parquet_stats_adj_factor(),
        "calendar": store.parquet_stats_calendar(),
        "index": store.parquet_stats_index(),
        "industry": store.parquet_stats_industry(),
        "stock_basic": store.parquet_stats_stock_basic(),
        "sources": sources.health_snapshot(),
    }


@router.get("/bs_monitor")
def bs_monitor(_user: str = Depends(get_current_user)):
    """baostock API 调用监控：今日用量 vs 上限 / 并发连接 / 黑名单状态 / 出口IP"""
    from ..data.bs_usage import tracker
    return tracker.get_monitor()


@router.post("/bs_check")
def bs_check(_user: str = Depends(get_current_user)):
    """立即做一次 baostock 健康检查，主动探测是否被黑名单（登录/查询返回 10001011）"""
    from ..data.bs_usage import tracker
    bs_src = next((s for s in sources.SOURCES if s.name == "baostock"), None)
    try:
        ok = bool(bs_src and bs_src.health_check(timeout=15))
    except Exception:
        ok = False
    tracker.touch_check()
    return {"ok": ok, "monitor": tracker.get_monitor()}


class UpdateRequest(BaseModel):
    scope: str = "daily"
    stocks: Optional[list[str]] = None  # 指定股票（sh.600021/600021 均可）；空=全量
    start_date: Optional[str] = None    # 拉取起始日 YYYY-MM-DD；空=全历史
    end_date: Optional[str] = None      # 拉取截止日 YYYY-MM-DD；空=全历史


@router.post("/update")
def data_update(req: UpdateRequest, _user: str = Depends(get_current_user)):
    if req.scope not in ("daily", "minute5", "all", "industry", "stock_basic", "calendar"):
        raise HTTPException(status_code=400, detail="scope 需为 daily|minute5|industry|stock_basic|calendar|all")
    if req.stocks is not None and not req.stocks:
        raise HTTPException(status_code=400, detail="stocks 为空时请勿传该字段（全量更新）")
    task_id = "data_" + uuid.uuid4().hex[:12]
    label = f"数据更新:{req.scope}" + (f"({len(req.stocks)}只)" if req.stocks else "")
    db.create_task(task_id, label, "data_update",
                   payload={"scope": req.scope, "stocks": req.stocks,
                            "start_date": req.start_date, "end_date": req.end_date})
    manager.submit("data_update", task_id, scope=req.scope, codes=req.stocks,
                   start_date=req.start_date or "1990-01-01",
                   end_date=req.end_date or "2099-12-31")
    return {"task_id": task_id, "status": "pending"}


class DemoRequest(BaseModel):
    stocks: Optional[list[str]] = None
    days: int = Field(default=500, ge=30, le=3000)


@router.post("/demo")
def data_demo(req: DemoRequest, _user: str = Depends(get_current_user)):
    task_id = "data_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, "生成演示数据", "data_update",
                   payload={"scope": "demo", "stocks": req.stocks, "days": req.days})
    manager.submit("data_demo", task_id, stocks=req.stocks, days=req.days)
    return {"task_id": task_id, "status": "pending"}
