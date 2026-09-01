# -*- coding: utf-8 -*-
"""股票查询：本地 stock_basic.parquet 模糊匹配 code 或 name；
条件选股：指数成分 / 申万行业 / 板块 过滤 + 可复现随机抽样（UNIVERSE_PICKER 方案 §6）。"""
from datetime import datetime
from typing import Optional, Union

import numpy as np
import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..data import sources, store

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("")
def search_stocks(keyword: str = Query(default=""), limit: int = Query(default=20, ge=1, le=100),
                  _user: str = Depends(get_current_user)):
    basic = store.read_stock_basic()
    if basic is None or basic.height == 0:
        return []
    # 股票池排除 ST 股与退市股（前端手动选择也不展示）
    basic = basic.filter((~pl.col("st")) & (~pl.col("delisted")))
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
    df = basic.filter(pl.col("code").is_in(wanted)
                      & (~pl.col("st")) & (~pl.col("delisted")))
    rows = df.select(["code", "name", "st"]).to_dicts()
    order = {c: i for i, c in enumerate(wanted)}
    rows.sort(key=lambda r: order.get(r["code"], 10**9))
    return [{"code": r["code"], "name": r["name"], "st": bool(r["st"])} for r in rows]


# ---------------- 条件选股（UNIVERSE_PICKER） ----------------

def _snapshot(df: Optional[pl.DataFrame]) -> Optional[str]:
    """取快照日期（两表任一缺失 -> None，前端引导去数据管理页更新）"""
    if df is None or df.height == 0 or "snapshot_date" not in df.columns:
        return None
    return str(df["snapshot_date"].max())


def _build_industry_tree(df: pl.DataFrame) -> list[dict]:
    """stock_industry -> L1→L2→L3 树（value/label，节点带成分计数）"""
    df = df.filter((pl.col("sw_l1") != "") & (pl.col("sw_l2") != "")
                   & (pl.col("sw_l3") != ""))
    tree = []
    for r1 in (df.group_by("sw_l1").agg(pl.col("code").n_unique().alias("count"))
               .sort("sw_l1").to_dicts()):
        sub2 = df.filter(pl.col("sw_l1") == r1["sw_l1"]) \
                 .group_by("sw_l2").agg(pl.col("code").n_unique().alias("count")) \
                 .sort("sw_l2")
        children2 = []
        for r2 in sub2.to_dicts():
            sub3 = df.filter(pl.col("sw_l2") == r2["sw_l2"]) \
                     .group_by("sw_l3").agg(pl.col("code").n_unique().alias("count")) \
                     .sort("sw_l3")
            children3 = [{"value": r3["sw_l3"], "label": f"{r3['sw_l3']}({r3['count']})",
                          "count": int(r3["count"])} for r3 in sub3.to_dicts()]
            children2.append({"value": r2["sw_l2"], "label": f"{r2['sw_l2']}({r2['count']})",
                              "count": int(r2["count"]), "children": children3})
        tree.append({"value": r1["sw_l1"], "label": f"{r1['sw_l1']}({r1['count']})",
                     "count": int(r1["count"]), "children": children2})
    return tree


@router.get("/pick-options")
def pick_options(_user: str = Depends(get_current_user)):
    """返回条件选股的筛选维度选项（指数/行业树/板块）+ 快照日期。"""
    idx = store.read_index_constituents()
    ind = store.read_stock_industry()
    basic = store.read_stock_basic()

    indices = []
    if idx is not None and idx.height:
        for key, (_, name) in sources.INDEX_REGISTRY.items():
            cnt = idx.filter(pl.col("index_key") == key).height
            indices.append({"key": key, "name": name, "count": int(cnt)})
        cnt = idx.filter(pl.col("index_key") == sources.INDEX_CSI800).height
        indices.append({"key": sources.INDEX_CSI800, "name": sources.INDEX_CSI800_NAME,
                        "count": int(cnt)})

    industry_tree = _build_industry_tree(ind) if ind is not None and ind.height else []

    boards = []
    if basic is not None and basic.height:
        counts = {k: 0 for k in sources.BOARD_LABELS}
        for c in basic["code"].to_list():
            b = sources.derive_board(c)
            if b in counts:
                counts[b] += 1
        for key, label in sources.BOARD_LABELS.items():
            boards.append({"key": key, "name": label, "count": counts[key]})

    return {
        "indices": indices,
        "industry_tree": industry_tree,
        "boards": boards,
        "industry_snapshot": _snapshot(ind),
        "index_snapshot": _snapshot(idx),
    }


class MomentumPick(BaseModel):
    """动量趋势预筛（MOMENTUM_CORE 同口径：门槛 -> RPS -> 排序 -> 取前 x）"""
    top_x: int = Field(default=30, ge=1, le=500)      # 排序后取前 x 只
    above_ma: int = Field(default=60, ge=5, le=120)   # 站上均线锚周期（60/20 对齐两策略）
    with_accel: bool = False                          # 动量分叠加加速度项（对齐 momentum_slot）
    min_rps: Optional[float] = Field(default=None, ge=0, le=100)  # 全市场分位下限


class PickFilters(BaseModel):
    # 指数成分：单选或多选（多选=并集）sz50|hs300|zz500|csi800；兼容历史单字符串
    index: Optional[Union[str, list[str]]] = None
    industry_l1: list[str] = Field(default_factory=list)
    industry_l2: list[str] = Field(default_factory=list)
    industry_l3: list[str] = Field(default_factory=list)
    boards: list[str] = Field(default_factory=list)
    exclude_st: bool = True
    momentum: Optional[MomentumPick] = None   # 动量趋势预筛（需配合 as_of）


class PickRandom(BaseModel):
    n: Optional[int] = None                   # 抽取数量；缺省=全量
    seed: Optional[int] = None                # 随机种子；缺省=后端生成


class PickRequest(BaseModel):
    filters: PickFilters = Field(default_factory=PickFilters)
    random: Optional[PickRandom] = None
    as_of: Optional[str] = None               # 动量预筛基准日（传回测开始日，取其前一交易日）


def _pick_matched(filters: PickFilters) -> tuple[list[str], dict[str, str]]:
    """按过滤条件求命中股票（维度间 AND、维度内 OR），返回 (codes, name_map)。
    始终排除退市股；ST 按 exclude_st 开关（默认开）。"""
    basic = store.read_stock_basic()
    if basic is None or basic.height == 0:
        return [], {}
    codes: set[str] = set(basic["code"].to_list())
    name_map = {r["code"]: r["name"] for r in basic.select(["code", "name"]).to_dicts()}

    # 退市股（delisted）无条件排除
    codes -= set(basic.filter(pl.col("delisted"))["code"].to_list())

    idx_keys = filters.index
    if isinstance(idx_keys, str):
        idx_keys = [idx_keys]
    idx_keys = [k.strip() for k in (idx_keys or []) if k and k.strip()]
    if idx_keys:
        idx = store.read_index_constituents()
        if idx is None or idx.height == 0:
            raise HTTPException(status_code=400, detail="指数成分数据未就绪，请先在数据管理页更新行业与成分")
        allowed = {sources.INDEX_CSI800, *sources.INDEX_REGISTRY}
        bad = [k for k in idx_keys if k not in allowed]
        if bad:
            raise HTTPException(status_code=400, detail=f"未知指数: {bad}")
        # 多选=并集（维度内 OR）；与行业/板块维度间 AND
        idx_codes = set(idx.filter(pl.col("index_key").is_in(idx_keys))["code"].to_list())
        codes &= idx_codes

    l1 = [s.strip() for s in filters.industry_l1 if s.strip()]
    l2 = [s.strip() for s in filters.industry_l2 if s.strip()]
    l3 = [s.strip() for s in filters.industry_l3 if s.strip()]
    if l1 or l2 or l3:
        ind = store.read_stock_industry()
        if ind is None or ind.height == 0:
            raise HTTPException(status_code=400, detail="申万行业数据未就绪，请先在数据管理页更新行业与成分")
        cond = (pl.lit(False))
        if l1:
            cond |= pl.col("sw_l1").is_in(l1)
        if l2:
            cond |= pl.col("sw_l2").is_in(l2)
        if l3:
            cond |= pl.col("sw_l3").is_in(l3)
        codes &= set(ind.filter(cond)["code"].to_list())

    boards = [s.strip() for s in filters.boards if s.strip()]
    if boards:
        valid = {*sources.BOARD_LABELS}
        bad = [b for b in boards if b not in valid]
        if bad:
            raise HTTPException(status_code=400, detail=f"未知板块: {bad}")
        codes = {c for c in codes if sources.derive_board(c) in boards}

    if filters.exclude_st:
        st_codes = set(basic.filter(pl.col("st") == True)["code"].to_list())  # noqa: E712
        codes -= st_codes

    return sorted(codes), name_map


@router.post("/pick")
def pick_stocks(req: PickRequest, _user: str = Depends(get_current_user)):
    """条件选股（即时查询）：过滤 + 可复现随机抽样 / 动量趋势预筛。
    同 seed 必然同池子（sorted(codes) 后 np.random.default_rng(seed).choice）。"""
    mo = req.filters.momentum
    if mo:
        return _pick_momentum(req, mo)
    codes, name_map = _pick_matched(req.filters)
    total_matched = len(codes)
    n = req.random.n if req.random else None
    seed = req.random.seed if req.random else None

    total_picked = total_matched
    truncated = False
    seed_used = None
    if total_matched:
        if n is not None and n > 0 and n < total_matched:
            if seed is None:
                seed = int(np.random.default_rng().integers(0, 2**31 - 1))
            picked = np.random.default_rng(seed).choice(codes, n, replace=False)
            codes = sorted(picked.tolist())
            total_picked = len(codes)
            seed_used = seed
            truncated = False
        elif n is not None and n > 0:
            # n >= 命中数：全取并在 meta 提示
            truncated = True

    filters_dict = req.filters.model_dump()
    meta = {
        "source": "industry_pick",
        "filters": filters_dict,
        "seed_used": seed_used,
        "total_matched": total_matched,
        "picked_at": datetime.now().strftime("%Y-%m-%d"),
    }
    return {
        "codes": codes,
        "name_map": {c: name_map.get(c, "") for c in codes},
        "total_matched": total_matched,
        "total_picked": total_picked,
        "seed_used": seed_used,
        "truncated": truncated,
        "meta": meta,
    }


def _pick_momentum(req: PickRequest, mo: MomentumPick) -> dict:
    """动量趋势预筛：全市场（或静态过滤域内）按 MOMENTUM_CORE 同口径
    「门槛 -> RPS -> 排序 -> 取前 x」。

    无后视镜：as_of 传回测开始日，实际基准日 = 严格早于它的最近交易日，
    只使用该日收盘信息（与策略 T-1 建仓语义一致）。
    静态维度（指数/行业/板块）作为候选域叠加；RPS 分位恒为全市场口径。
    """
    from ..engine import momentum_core as mc

    if not req.as_of:
        raise HTTPException(status_code=400,
                            detail="动量预筛需先确认回测时间范围（as_of=回测开始日）")
    try:
        datetime.strptime(req.as_of, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="as_of 需为 YYYY-MM-DD 格式")
    # 静态过滤域（可选）：指数/行业/板块/ST 命中集
    domain: set | None = None
    f = req.filters
    has_static = bool(f.index or f.industry_l1 or f.industry_l2 or f.industry_l3
                      or f.boards)
    if has_static:
        static_codes, _ = _pick_matched(f)
        if not static_codes:
            raise HTTPException(status_code=400, detail="静态过滤条件下无候选股票")
        domain = set(static_codes)

    # 特征窗口：基准日前推约 280 个交易日（自然日近似），覆盖最长回看参数
    win_start = (datetime.strptime(req.as_of, "%Y-%m-%d").toordinal() - 427)
    win_start = datetime.fromordinal(win_start).strftime("%Y-%m-%d")
    pp = mc.pick_params(above_ma=mo.above_ma, with_accel=mo.with_accel)
    try:
        mf = mc.market_features(window_start=win_start, window_end=req.as_of, p=pp)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    as_of = mc.as_of_before(mf, req.as_of)
    if as_of is None:
        raise HTTPException(status_code=400,
                            detail=f"基准日缺失：{req.as_of} 之前无行情数据，请先更新日线")
    picked = mc.select_top(mf, as_of, mo.top_x, mo.min_rps, domain=domain)
    basic = store.read_stock_basic()
    name_map = ({r["code"]: r["name"] for r in basic.select(["code", "name"]).to_dicts()}
                if basic is not None and basic.height else {})
    codes = picked["code"].to_list()
    items = [{"rank": int(r["rank"]), "code": r["code"],
              "name": name_map.get(r["code"], r["code"]),
              "score": round(float(r["score"]), 4),
              "rps": (round(float(r["rps"]) * 100, 1)
                      if r.get("rps") is not None else None)}
             for r in picked.to_dicts()]
    meta = {
        "source": "momentum_pick",
        "as_of_requested": req.as_of,
        "snapshot_date": as_of,          # 实际基准日（无后视镜）
        "momentum": mo.model_dump(),
        "domain": sorted(domain) if domain else None,
        "total_matched": int(picked.height),
        "picked_at": datetime.now().strftime("%Y-%m-%d"),
    }
    return {
        "codes": codes,
        "name_map": {c: name_map.get(c, "") for c in codes},
        "total_matched": int(picked.height),
        "total_picked": int(picked.height),
        "seed_used": None,
        "truncated": False,
        "items": items,
        "meta": meta,
    }
