# -*- coding: utf-8 -*-
"""AI 分析接口（多 LLM）"""
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import config, db
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
    # 若存在该回测的敏感度扫描结果（Phase 3），自动附加实测表
    sensitivity = _latest_sensitivity(req.backtest_id)
    manager.submit("ai", task_id, backtest_id=req.backtest_id,
                   profile=req.profile, param_importance=param_importance,
                   username=user, sensitivity=sensitivity)
    return {"task_id": task_id, "status": "pending"}


def _latest_sensitivity(backtest_id: str):
    """该回测最近一次成功的敏感度扫描结果（Phase 3，无则 None）"""
    from ..llm.sensitivity import load_latest_sensitivity
    try:
        return load_latest_sensitivity(backtest_id, str(config.REPORTS_DIR))
    except Exception:  # noqa: BLE001
        return None


class SensitivityBody(BaseModel):
    backtest_id: str
    params: Optional[list[str]] = None  # 缺省自动选（param_importance top / schema 前序）


@router.post("/sensitivity")
def run_sensitivity(body: SensitivityBody, user: str = Depends(get_current_user)):
    """方案 B Phase 3 敏感度扫描：关键参数 ±20% 网格各跑一次回测 → 实测表。
    成本护栏：参数 ≤4、每参数 ≤5 值、总回测 ≤20。结果在后续 AI 分析时自动附加。"""
    bt = db.get_task(body.backtest_id)
    if bt is None or bt["status"] != "success":
        raise HTTPException(status_code=400, detail="回测任务不存在或未成功")
    keys = [str(k) for k in (body.params or []) if k]
    if len(keys) > 4:
        raise HTTPException(status_code=400,
                            detail="扫描参数过多（≤4 个），请用 param_importance 优先级裁剪")
    if not keys:
        # 自动选参：寻优重要性 → 报告内寻优摘要 → schema 前序
        keys = _auto_scan_params(body.backtest_id)
        if not keys:
            raise HTTPException(status_code=400, detail="未能自动确定扫描参数，请显式指定 params")
    new_task_id = "sen_" + uuid.uuid4().hex[:12]
    db.create_task(new_task_id, f"敏感度扫描:{body.backtest_id}", "ai_sensitivity",
                   payload={"backtest_id": body.backtest_id, "params": keys})
    manager.submit("ai_sensitivity", new_task_id, backtest_id=body.backtest_id,
                   params=keys)
    return {"task_id": new_task_id, "status": "pending", "params": keys}


def _auto_scan_params(backtest_id: str) -> list[str]:
    """自动选参：任务 payload/寻优报告的 param_importance top3 → 失败返回空"""
    from ..llm.sensitivity import auto_pick_params
    bt = db.get_task(backtest_id)
    report_path = (bt.get("payload") or {}).get("report_path") if bt else None
    if not report_path or not Path(report_path).exists():
        return []
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not report.get("_param_importance"):
        report["_param_importance"] = _latest_param_importance(
            (report.get("config") or {}).get("strategy_id")) or {}
    return auto_pick_params(report, limit=3)


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


class RefineBody(BaseModel):
    profile: Optional[str] = None  # auto(默认) | 服务商名 | key_id（数字字符串）


@router.post("/analyses/{task_id}/refine")
def refine_analysis(task_id: str, body: RefineBody,
                    user: str = Depends(get_current_user)):
    """方案 B Phase 2 二轮修正：基于原分析的实测验证结果让 LLM 修正建议，
    修正建议自动再验证，落库为新 analysis（refined_from 指向原分析）。
    单步限制：修正产物不可再修正。"""
    analysis = db.get_analysis_by_task(task_id)
    if analysis is None or analysis["status"] != "success":
        raise HTTPException(status_code=404, detail="分析不存在或未成功")
    if analysis.get("refined_from"):
        raise HTTPException(status_code=400,
                            detail="该分析已是修正产物，不支持二次修正（单步限制）")
    if not analysis.get("suggestions"):
        raise HTTPException(status_code=400, detail="原分析没有结构化建议，无需修正")
    if not analysis.get("validation") or analysis["validation"].get("error"):
        raise HTTPException(status_code=400, detail="原分析缺少有效的验证结果，无法修正")
    # 发起人未配置任何可用 key 且系统级兜底也为空 → 提前友好报错
    if not provider.db_key_entries(user) and not provider.key_pool_mode():
        available = [p["name"] for p in provider.profiles_info(user)["profiles"]
                     if p["available"]]
        if not available:
            raise HTTPException(
                status_code=400,
                detail="未配置 LLM API Key：请到「Key 管理」页添加你的 API Key")
    new_task_id = "ai_" + uuid.uuid4().hex[:12]
    db.create_task(new_task_id, f"AI修正:{task_id}", "ai_refine",
                   payload={"refine_from": task_id, "profile": body.profile,
                            "username": user})
    manager.submit("ai_refine", new_task_id, refine_from=task_id,
                   profile=body.profile, username=user)
    return {"task_id": new_task_id, "status": "pending"}
