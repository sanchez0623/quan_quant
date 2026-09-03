# -*- coding: utf-8 -*-
"""实盘信号机 API（LIVE_SIGNAL_SYSTEM）：盘前/盘中/盘后全流程 + 信号/回填/
持仓对账/配置 + M3 影子统计/M4 就绪检查"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import get_current_user
from ..live import feishu, intraday, premarket, reports
from ..task_manager import manager

router = APIRouter(prefix="/api/live", tags=["live"])

ALLOWED_SIGNAL_STATUS = ("待执行", "已成交", "已忽略", "已过期", "信息")


class SignalStatusBody(BaseModel):
    status: str


@router.post("/premarket")
def run_premarket_now(_user: str = Depends(get_current_user)):
    """触发盘前信号流程（T-1 特征重算/重选判定/gate/退出检查/飞书推送）"""
    try:
        result = premarket.run_premarket()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/signals")
def list_signals(limit: int = 100, status: Optional[str] = None,
                 _user: str = Depends(get_current_user)):
    return db.list_live_signals(limit=limit, status=status)


@router.post("/signals/{signal_id}/status")
def set_signal_status(signal_id: int, body: SignalStatusBody,
                      _user: str = Depends(get_current_user)):
    if body.status not in ALLOWED_SIGNAL_STATUS:
        raise HTTPException(status_code=400,
                            detail=f"status 需为 {list(ALLOWED_SIGNAL_STATUS)} 之一")
    if not db.set_live_signal_status(signal_id, body.status):
        raise HTTPException(status_code=404, detail=f"信号不存在: {signal_id}")
    return {"id": signal_id, "status": body.status}


class FillBody(BaseModel):
    signal_id: Optional[int] = None
    code: str
    side: str = Field(pattern="^(buy|sell)$")
    fill_price: float = Field(gt=0)
    fill_volume: int = Field(gt=0)
    fee: float = 0.0
    fill_time: Optional[str] = None
    note: str = ""


@router.post("/fills")
def add_fill(body: FillBody, _user: str = Depends(get_current_user)):
    """回填实际成交：记流水 + 联动虚拟持仓（buy 建仓/加仓，sell 减仓/清仓）+
    关联信号置为已成交"""
    fid = db.add_live_fill(body.signal_id, body.code, body.side,
                           body.fill_price, body.fill_volume, body.fee,
                           body.fill_time, body.note)
    pos = {p["code"]: p for p in db.list_live_positions()}
    if body.side == "buy":
        old = pos.get(body.code)
        if old and old["volume"] > 0:
            new_vol = old["volume"] + body.fill_volume
            cost = (old["cost_price"] * old["volume"]
                    + body.fill_price * body.fill_volume) / new_vol
            db.upsert_live_position(body.code, old["name"], new_vol,
                                    round(cost, 4), old.get("open_day"),
                                    old.get("group_id"))
        else:
            name = (db.list_live_signals(limit=500) and
                    next((s["name"] for s in db.list_live_signals(limit=500)
                          if s["code"] == body.code and s["name"]), body.code))
            db.upsert_live_position(body.code, name, body.fill_volume,
                                    body.fill_price,
                                    (body.fill_time or "")[:10] or None)
    else:
        old = pos.get(body.code)
        if old:
            remain = old["volume"] - body.fill_volume
            if remain <= 0:
                db.remove_live_position(body.code)
            else:
                db.upsert_live_position(body.code, old["name"], remain,
                                        old["cost_price"], old.get("open_day"),
                                        old.get("group_id"))
        else:
            raise HTTPException(status_code=400,
                                detail=f"卖出回填失败：虚拟持仓无 {body.code}")
    if body.signal_id:
        db.set_live_signal_status(body.signal_id, "已成交")
    return {"fill_id": fid, "positions": db.list_live_positions()}


@router.get("/positions")
def list_positions(_user: str = Depends(get_current_user)):
    return db.list_live_positions()


class SyncBody(BaseModel):
    """以券商实际持仓为准重建虚拟持仓（每日对账校准）"""
    positions: list[dict] = Field(default_factory=list)  # [{code,name,volume,cost_price}]


@router.post("/positions/sync")
def sync_positions(body: SyncBody, _user: str = Depends(get_current_user)):
    for p in db.list_live_positions():
        db.remove_live_position(p["code"])
    for p in body.positions:
        if p.get("code") and int(p.get("volume") or 0) > 0:
            db.upsert_live_position(str(p["code"]), p.get("name") or str(p["code"]),
                                    int(p["volume"]), float(p.get("cost_price") or 0),
                                    p.get("open_day"))
    return {"positions": db.list_live_positions()}


@router.get("/config")
def get_config(_user: str = Depends(get_current_user)):
    return {**premarket.DEFAULT_CFG, **db.get_live_config()}


class LiveConfigBody(BaseModel):
    above_ma: int = 20
    with_accel: bool = True
    rank_key: str = "score"
    top_x: int = 30
    auto_idle_days: int = 5
    exit_need: int = 2
    enter_th: float = 0.15
    pool_n: int = 6
    min_rps: Optional[float] = None
    initial_capital: float = 3_000_000.0
    suggest_pct: float = 0.15
    auto_index: list[str] = Field(default_factory=list)
    auto_boards: list[str] = Field(default_factory=list)
    t_mode: str = "off"
    max_holdings: int = 3


@router.post("/config")
def save_config(body: LiveConfigBody, _user: str = Depends(get_current_user)):
    cfg = body.model_dump()
    db.save_live_config(cfg)
    return cfg


@router.get("/summary")
def live_summary(_user: str = Depends(get_current_user)):
    """概览：池子/gate/持仓/最近信号/推送配置（前端首页）"""
    pool = db.get_live_pool()
    return {
        "pool": pool,
        "positions": db.list_live_positions(),
        "signals": db.list_live_signals(limit=50),
        "fills": db.list_live_fills(limit=50),
        "feishu_configured": feishu.configured(),
        "config": {**premarket.DEFAULT_CFG, **db.get_live_config()},
    }


class ResetBody(BaseModel):
    keep_config: bool = True   # 保留流程参数配置


@router.post("/reset")
def reset_live(body: ResetBody, _user: str = Depends(get_current_user)):
    """清空实盘信号机数据（信号/回填/虚拟持仓/池子状态/盘中状态机快照），重新开始"""
    db.reset_live_data(keep_config=body.keep_config)
    return {"reset": True, "keep_config": body.keep_config}


# ---------------- M2 盘中信号机 ----------------

class MorningBody(BaseModel):
    update_data: bool = True   # 先做日线增量更新（含完整性守卫）
    push: bool = True


@router.post("/morning")
def morning_run(body: MorningBody, _user: str = Depends(get_current_user)):
    """盘前编排任务（异步）：日线增量更新 → 盘前信号流程。
    进度在任务中心查看；数据更新全市场约数分钟。"""
    task_id = "live_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, "实盘盘前流程" + ("（含拉数据）" if body.update_data else ""),
                   "live_premarket",
                   payload={"update_data": body.update_data, "push": body.push})
    manager.submit("live_premarket", task_id,
                   update_data=body.update_data, push=body.push)
    return {"task_id": task_id, "status": "pending"}


@router.post("/intraday")
def intraday_run(_user: str = Depends(get_current_user)):
    """执行一次盘中轮询：完成 bar → 状态机步进 → 风控前置 → 推送/落库。
    幂等（bar 游标去重），盘中可反复调用（建议 60 秒一轮）。"""
    return intraday.run_intraday(push=True)


@router.get("/intraday/status")
def intraday_status(_user: str = Depends(get_current_user)):
    """盘中控制台快照：各票实时价/状态机状态/喂bar游标/心跳（轻量，不拉K线）"""
    return intraday.status_snapshot()


@router.post("/postclose")
def postclose_run(_user: str = Depends(get_current_user)):
    """盘后流程（任务化，防止逐码拉行情期间请求超时/中断）：
    当日分钟线合并落库（池子∪持仓∪跟踪）+ 对账卡推送；返回 {task_id}"""
    task_id = "live_" + uuid.uuid4().hex[:12]
    db.create_task(task_id, "实盘盘后流程", "live_postclose", payload={"push": True})
    manager.submit("live_postclose", task_id, push=True)
    return {"task_id": task_id, "status": "pending"}


# ---------------- M3 影子运行 / M4 就绪 ----------------

@router.get("/slippage")
def slippage(_user: str = Depends(get_current_user)):
    """滑点统计：实际成交价 vs 信号参考价（方向折算为滑点成本）"""
    return reports.slippage_stats()


@router.get("/shadow")
def shadow(_user: str = Depends(get_current_user)):
    """M3 影子运行统计：信号执行率 + 影子账户（全按参考价足额执行）vs 实际回填"""
    return reports.shadow_stats()


@router.get("/readiness")
def readiness(_user: str = Depends(get_current_user)):
    """M4 小资金实盘就绪检查清单（飞书/数据/行情源/影子时长/滑点样本/护栏）"""
    return reports.readiness()
