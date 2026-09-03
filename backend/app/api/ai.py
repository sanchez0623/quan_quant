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
def llm_profiles(user: str = Depends(get_current_user)):
    info = provider.profiles_info(user)
    return {"mode": info["mode"], "user_key_pool": info["user_key_pool"],
            "key_pool": info["key_pool"], "providers": info["providers"],
            "profiles": info["profiles"], "default": info["default"],
            "usage": db.llm_usage_stats()}


@router.delete("/usage")
def clear_usage(_user: str = Depends(get_current_user)):
    """清空 LLM 用量统计（如清除测试期产生的脏数据）"""
    db.clear_llm_usage()
    return {"status": "ok"}


class AnalyzeRequest(BaseModel):
    backtest_id: str
    profile: Optional[str] = None  # auto(默认) | 服务商名 | key_id（数字字符串）


@router.post("/analyze")
def create_analysis(req: AnalyzeRequest, user: str = Depends(get_current_user)):
    bt = db.get_task(req.backtest_id)
    if bt is None or bt["status"] != "success":
        raise HTTPException(status_code=400, detail="回测任务不存在或未成功")
    # 发起人未配置任何可用 key 且系统级兜底也为空 → 提前友好报错
    if not provider.db_key_entries(user) and not provider.key_pool_mode():
        available = [p["name"] for p in provider.profiles_info(user)["profiles"] if p["available"]]
        if not available:
            raise HTTPException(
                status_code=400,
                detail="未配置 LLM API Key：请到「Key 管理」页添加你的 API Key（支持 DeepSeek/"
                       "OpenRouter/火山方舟/智谱等，可配多个自动切换）")
    task_id = "ai_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, f"AI分析:{req.backtest_id}", "ai",
                   payload={"backtest_id": req.backtest_id, "profile": req.profile,
                            "username": user})
    # 若存在同策略的寻优结果，附加参数重要性
    param_importance = _latest_param_importance(bt.get("payload", {}).get("strategy_id"))
    manager.submit("ai", task_id, backtest_id=req.backtest_id,
                   profile=req.profile, param_importance=param_importance, username=user)
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


@router.get("/suggestion-stats")
def suggestion_stats(_user: str = Depends(get_current_user)):
    """AI 建议验证胜率统计：全部分析的建议验证结论（改善/持平/恶化）计数。"""
    return db.ai_verdict_stats()
