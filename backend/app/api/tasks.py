# -*- coding: utf-8 -*-
"""任务通用操作：协作式取消（对全部任务类型生效——回测/寻优/AI/数据/实盘编排）。

取消语义：标记 cancelling 后，子进程在每个进度检查点（update_progress）
感知并自杀，由 run_task 统一落 cancelled 终态——不在写库中途强杀进程，
数据一致性由各更新函数的原子写（parquet 原子替换 / SQLite 事务）保证。
取消延迟 = 到下一个进度检查点的距离（数据更新为逐码粒度，秒级）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# 定时调度链会提交的任务类型（定时任务 tab 聚合展示）
SCHEDULE_TASK_TYPES = ["data_update", "live_premarket", "live_postclose"]


@router.get("/schedule-status")
def schedule_status(_user: str = Depends(get_current_user)):
    """定时任务页：调度开关 + 今日各定时任务提交状态 + 最近调度类任务列表。

    submitted_today 与 scheduler 的当日幂等标记同源（auto_{kind}_date），
    minute5 为日线任务终态后的跟随提交标记。"""
    today = date.today().isoformat()
    cfg = db.get_live_config()
    submitted = {k: db.get_meta(f"auto_{k}_date") == today
                 for k in ("morning", "postclose", "evening", "minute5")}
    daily_id = db.get_meta("auto_evening_daily_id")
    daily_task = db.get_task(daily_id) if daily_id else None
    tasks = db.list_tasks(types=SCHEDULE_TASK_TYPES, limit=50)
    return {
        "auto_schedule": bool(cfg.get("auto_schedule", True)),
        "today": today,
        "submitted_today": submitted,
        "evening_daily_id": daily_id,
        "evening_daily_status": (daily_task or {}).get("status"),
        "tasks": tasks,
    }


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, _user: str = Depends(get_current_user)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] in ("success", "failed", "cancelled"):
        raise HTTPException(status_code=400,
                            detail=f"任务已结束（{task['status']}），无需取消")
    db.request_cancel(task_id)
    return {"task_id": task_id, "status": "cancelling",
            "note": "已请求停止，任务将在当前进度检查点退出"}
