# -*- coding: utf-8 -*-
"""AI 分析接口（多 LLM）"""
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..auth import get_current_user
from ..llm import provider
from ..task_manager import manager

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/profiles")
def llm_profiles(_user: str = Depends(get_current_user)):
    info = provider.profiles_info()
    return {"profiles": info["profiles"], "default": info["default"],
            "usage": db.llm_usage_stats()}


class AnalyzeRequest(BaseModel):
    backtest_id: str
    profile: Optional[str] = None


@router.post("/analyze")
def create_analysis(req: AnalyzeRequest, _user: str = Depends(get_current_user)):
    bt = db.get_task(req.backtest_id)
    if bt is None or bt["status"] != "success":
        raise HTTPException(status_code=400, detail="回测任务不存在或未成功")
    if req.profile and not provider.profile_available(req.profile):
        # 未配置 key 的 profile 交给任务层给出友好错误（沿 fallback 尝试）
        pass
    task_id = "ai_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, f"AI分析:{req.backtest_id}", "ai",
                   payload={"backtest_id": req.backtest_id, "profile": req.profile})
    # 若存在同策略的寻优结果，附加参数重要性
    param_importance = _latest_param_importance(bt.get("payload", {}).get("strategy_id"))
    manager.submit("ai", task_id, backtest_id=req.backtest_id,
                   profile=req.profile, param_importance=param_importance)
    return {"task_id": task_id, "status": "pending"}


def _latest_param_importance(strategy_id: Optional[str]) -> Optional[dict]:
    for t in db.list_tasks("optimize"):
        if t["status"] != "success":
            continue
        if strategy_id and t.get("payload", {}).get("strategy_id") not in (None, strategy_id):
            continue
        path = t.get("payload", {}).get("report_path") or db.get_report_path(t["task_id"])
        if path and Path(path).exists():
            try:
                summary = json.loads(Path(path).read_text(encoding="utf-8"))
                if summary.get("param_importance"):
                    return summary["param_importance"]
            except (json.JSONDecodeError, OSError):
                continue
    return None


@router.get("/analyses")
def list_analyses(backtest_id: Optional[str] = Query(default=None),
                  _user: str = Depends(get_current_user)):
    return db.list_analyses(backtest_id)
