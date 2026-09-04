# -*- coding: utf-8 -*-
"""任务通用操作：协作式取消（对全部任务类型生效——回测/寻优/AI/数据/实盘编排）。

取消语义：标记 cancelling 后，子进程在每个进度检查点（update_progress）
感知并自杀，由 run_task 统一落 cancelled 终态——不在写库中途强杀进程，
数据一致性由各更新函数的原子写（parquet 原子替换 / SQLite 事务）保证。
取消延迟 = 到下一个进度检查点的距离（数据更新为逐码粒度，秒级）。"""
from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
