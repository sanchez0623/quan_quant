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


class ApplyBody(BaseModel):
    analysis_task_id: str
    mode: str = "backtest"  # backtest=合并后直接创建回测任务 | prefill=返回合并配置供表单预填


@router.post("/apply")
def apply_suggestions(body: ApplyBody, user: str = Depends(get_current_user)):
    """把 AI 建议合并进原回测配置（后端唯一合并实现，前后端口径统一）。

    - mode=backtest：merge → validate_backtest_config 完整校验 → 创建回测任务
    - mode=prefill：返回合并后的完整配置，前端预填回测表单人工确认
    """
    if body.mode not in ("backtest", "prefill"):
        raise HTTPException(status_code=400, detail="mode 需为 backtest / prefill")
    analysis = db.get_analysis_by_task(body.analysis_task_id)
    if analysis is None or analysis["status"] != "success":
        raise HTTPException(status_code=404, detail="分析不存在或未成功")
    if not analysis.get("suggestions"):
        raise HTTPException(status_code=400, detail="该分析没有结构化建议（无可应用项）")
    bt = db.get_task(analysis["backtest_id"])
    if bt is None or bt["status"] != "success":
        raise HTTPException(status_code=400, detail="原回测任务不存在或未成功")
    cfg = (bt.get("payload") or {}).get("config")
    if not isinstance(cfg, dict) or not cfg.get("strategy_id"):
        raise HTTPException(status_code=400, detail="原回测配置缺失，无法合并")
    from ..llm.validation import merge_suggestions
    merged = merge_suggestions(cfg, analysis["suggestions"])
    merged["name"] = f"{cfg.get('name') or '回测任务'}-AI优化"
    # 完整校验（参数范围/动态选股约束/日期等），与 POST /api/backtests 同口径
    from .backtests import validate_backtest_config
    merged = validate_backtest_config(merged)
    if body.mode == "prefill":
        return {"mode": "prefill", "config": merged}
    task_id = "bt_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, merged.get("name") or "回测任务", "backtest",
                   payload={"strategy_id": merged["strategy_id"],
                            "period": merged.get("period", "daily"),
                            "config": merged})
    manager.submit("backtest", task_id, backtest_config=merged)
    return {"mode": "backtest", "task_id": task_id, "status": "pending"}
