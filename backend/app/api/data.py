# -*- coding: utf-8 -*-
"""数据管理接口：状态 / 增量更新 / 演示数据 / 完整性自检"""
import uuid
from datetime import datetime
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
        "index_daily": store.parquet_stats_index_daily(),
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
    if req.scope not in ("daily", "minute5", "all", "industry", "stock_basic",
                         "calendar", "index_daily"):
        raise HTTPException(status_code=400,
                            detail="scope 需为 daily|minute5|industry|stock_basic|calendar|index_daily|all")
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


# ---------------------------------------------------------------------------
# 数据完整性自检：覆盖率缺口 + 价格/复权因子突变（DATA_INTEGRITY）
# 背景：缺日线/复权因子错位会静默污染回测 pnl（2026-09 复权因子 fill_null 事故）。
# 规则均为「相邻日相对变化」，避开不同数据源复权口径差异。
# ---------------------------------------------------------------------------
class IntegrityRequest(BaseModel):
    codes: Optional[list[str]] = None              # 指定股票（空=全市场）
    start: Optional[str] = None                    # YYYY-MM-DD；空=最近 250 交易日
    end: Optional[str] = None                      # YYYY-MM-DD；空=日历最后一日
    gap_days: int = Field(default=10, ge=2, le=60)      # 缺口阈值（交易日）
    price_jump_pct: float = Field(default=25.0, ge=5, le=100)  # 价格突变阈值 %
    top_n: int = Field(default=50, ge=1, le=300)


def _empty_integrity(reason: str) -> dict:
    return {
        "ok": False, "reason": reason, "window": None,
        "codes_checked": 0, "coverage": {"with_gap_codes": 0, "gap_count": 0},
        "gaps": [], "price_anomalies": [], "factor_anomalies": [],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/integrity")
def data_integrity(req: IntegrityRequest, _user: str = Depends(get_current_user)):
    import polars as pl
    from ..data import store as st

    daily = st.read_daily(None)
    cal = st.read_calendar()
    if daily is None or cal is None or not daily.height or not cal.height:
        return _empty_integrity("日线或交易日历缺失，请先更新数据")
    cal_dates = sorted(cal["date"].to_list())
    start = req.start or cal_dates[max(0, len(cal_dates) - 250)]
    end = req.end or cal_dates[-1]
    df = daily.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    if req.codes:
        df = df.filter(pl.col("code").is_in(req.codes))
    if df.height == 0:
        return _empty_integrity("窗口内无数据")

    # ---- 交易日索引：缺口按交易日计（避免周末/节假日误报） ----
    cal_df = pl.DataFrame({"date": cal_dates}).with_row_index("dix")
    d = df.select(["code", "date", "close"]).sort(["code", "date"])
    d = d.join(cal_df, on="date", how="left").with_columns([
        pl.col("dix").shift().over("code").alias("dix_prev"),
        pl.col("date").shift().over("code").alias("prev_date"),
        pl.col("close").shift().over("code").alias("prev_close"),
    ]).with_columns((pl.col("dix") - pl.col("dix_prev")).alias("gap_tdays"))

    # ---- 1) 覆盖率缺口：相邻 bar 间跳过 > gap_days 个交易日（窗口首行不计） ----
    gap_df = (d.filter(pl.col("dix_prev").is_not_null()
                       & (pl.col("gap_tdays") > req.gap_days))
               .select(["code", "prev_date", "date", "gap_tdays"])
               .sort("gap_tdays", descending=True).head(req.top_n))
    n_gap_codes = d.filter(pl.col("dix_prev").is_not_null()
                           & (pl.col("gap_tdays") > req.gap_days))["code"].n_unique()

    # ---- 2) 价格 / 复权因子突变（需 adj_factor；无 factor 的票不判价格突变，防除权误报） ----
    price_anom, factor_anom = [], []
    adj = st.read_adj_factor(None)
    if adj is not None and adj.height:
        a = (df.select(["code", "date", "close"])
               .join(adj.select(["code", "date", "adj_factor"]),
                     on=["code", "date"], how="left")
               .sort(["code", "date"]).with_columns([
                   pl.col("close").shift().over("code").alias("prev_close"),
                   pl.col("adj_factor").shift().over("code").alias("prev_factor"),
               ]).with_columns([
                   ((pl.col("close") / pl.col("prev_close") - 1) * 100).alias("close_pct"),
                   ((pl.col("adj_factor") / pl.col("prev_factor") - 1) * 100).alias("factor_pct"),
               ]))
        # 价格异常：close 突变但 factor 几乎未变（非除权跳变）
        p = (a.filter(pl.col("prev_close").is_not_null()
                      & pl.col("prev_factor").is_not_null()
                      & (pl.col("close_pct").abs() > req.price_jump_pct)
                      & (pl.col("factor_pct").abs() < 5.0))
              .select(["code", "date", "prev_close", "close", "close_pct"])
              .sort("close_pct", descending=True).head(req.top_n))
        # 复权因子异常：factor 突变但 close 几乎未变
        f = (a.filter(pl.col("prev_factor").is_not_null()
                      & (pl.col("factor_pct").abs() > 30.0)
                      & (pl.col("close_pct").abs() < 5.0))
              .select(["code", "date", "prev_factor", "adj_factor", "factor_pct"])
              .sort("factor_pct", descending=True).head(req.top_n))
        price_anom, factor_anom = p.to_dicts(), f.to_dicts()

    return {
        "ok": True,
        "window": {"start": start, "end": end},
        "codes_checked": int(df["code"].n_unique()),
        "coverage": {"with_gap_codes": int(n_gap_codes), "gap_count": int(gap_df.height)},
        "gaps": gap_df.to_dicts(),
        "price_anomalies": price_anom,
        "factor_anomalies": factor_anom,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
