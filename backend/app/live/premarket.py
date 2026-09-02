# -*- coding: utf-8 -*-
"""盘前信号流程（LIVE_SIGNAL_SYSTEM §5 盘前，阶段 M1）。

每个交易日 08:30（或手动触发）：
1. 全市场 T-1 日线特征重算（momentum_core，与回测同口径；无后视镜）
2. 池级健康度 + gate 滞回状态机（历史滚动存 sig_pool）
3. 空仓重选判定（auto_idle_days）→ select_top 建仓名单（gate 停开仓时跳过）
4. 持仓票退出检查（exit_need 信号数：死叉/跌破均线/动量转负/跌出榜单）→ 预警
5. 消息组装 → 飞书推送 + sig_signal_log 落库 + sig_pool 状态滚动

参数从 sig_config 读（db.get_live_config），默认值对齐 momentum_slot。
"""
from datetime import datetime, timedelta
from typing import Optional

import polars as pl

from .. import db
from ..data import store
from ..engine import momentum_core as mc
from ..engine.runner import _auto_domain, _shift_back
from . import feishu

DEFAULT_CFG = {
    "above_ma": 20,          # 站上均线锚周期（20 对齐 momentum_slot）
    "with_accel": True,      # 动量分叠加加速度项
    "rank_key": "score",     # 选股排序键（score/accel/fresh/mom_gap）
    "top_x": 30,             # 每次预筛取前 x 只
    "auto_idle_days": 5,     # 全空仓持续 N 个交易日 -> 重选
    "exit_need": 2,          # 衰退信号满足数（预警阈值）
    "enter_th": 0.15,        # 池级 gate 触发阈值（恢复线=×2 内置）
    "pool_n": 6,             # 榜单容量（跌出榜单判定）
    "min_rps": None,         # 全市场 RPS 分位下限
    "initial_capital": 3_000_000.0,
    "suggest_pct": 0.15,     # 单票建议金额占虚拟权益比例
    "auto_index": ["zz500"],  # 候选域：指数成分并集
    "auto_boards": [],       # 候选域：板块并集
}


def _cfg() -> dict:
    return {**DEFAULT_CFG, **db.get_live_config()}


def run_premarket(data_dir: Optional[str] = None,
                  push: bool = True) -> dict:
    """执行盘前信号流程并推送。返回结果摘要（供 API/前端展示）。

    注意：本流程只读现有日线库、不拉数据——数据滞后时会在结果中警示
    （日线增量更新属数据管理链路，见 LIVE_SIGNAL_SYSTEM §5 盘前第 1 条，
     自动编排留待 M2 任务化）。"""
    cfg = _cfg()
    mf = mc.market_features(
        data_dir=data_dir,
        window_start=_shift_back(
            datetime.now().strftime("%Y-%m-%d"), 280),
        p=mc.pick_params(above_ma=int(cfg["above_ma"]),
                         with_accel=bool(cfg["with_accel"])))
    if not mf.calendar:
        raise RuntimeError("无行情数据（请先在数据管理页更新日线）")
    as_of = mf.calendar[-1]          # 数据最新日 = T-1（盘前无今日数据，无后视镜天然满足）

    # 参考价：as_of 日收盘（日线表原始 close）
    daily_close: dict[str, float] = {}
    try:
        dd = store.read_daily(None, data_dir)
        if dd is not None and dd.height:
            day_close = dd.filter(pl.col("date") == as_of).select(["code", "close"])
            daily_close = dict(zip(day_close["code"].to_list(),
                                   [float(x) for x in day_close["close"].to_list()]))
    except Exception:
        pass

    # 名称映射（stock_basic）；信号与池子展示用
    name_map: dict[str, str] = {}
    try:
        basic = store.read_stock_basic(data_dir)
        if basic is not None and basic.height:
            name_map = {r["code"]: r["name"]
                        for r in basic.select(["code", "name"]).to_dicts()
                        if r.get("name")}
    except Exception:
        pass

    # 数据滞后检测：数据截止日距今天 > 4 个自然日（跨长假）→ 警示
    stale_days = (datetime.now() - datetime.strptime(as_of, "%Y-%m-%d")).days
    stale = stale_days > 4

    pool_state = db.get_live_pool()
    pool = [p for p in (pool_state.get("pool") or [])]
    pool_codes = [p["code"] for p in pool]
    positions = db.list_live_positions()
    pos_codes = [p["code"] for p in positions]

    # ---- 1) 池级健康度 + gate 滞回状态机（滚动） ----
    day_feats = mf.feats.filter(
        (pl.col("day") == as_of)
        & pl.col("code").is_in(pool_codes)) if pool_codes else None
    health = None
    if day_feats is not None and day_feats.height:
        n = day_feats.filter(pl.col("score").is_not_null()).height
        pos = day_feats.filter((pl.col("score").is_not_null())
                               & (pl.col("score") > 0)).height
        health = round(pos / n, 4) if n else None

    history = [h for h in (pool_state.get("health_history") or [])
               if h.get("day") != as_of]
    if health is not None:
        history.append({"day": as_of, "health": health})
    history = history[-60:]
    enter_th = float(cfg["enter_th"])
    gate_map = mc._pool_gate_map([(h["day"], h["health"]) for h in history],
                                 enter_th) if history else {}
    gate_state = int(gate_map.get(as_of, 0)) if gate_map else \
        int(pool_state.get("gate_state") or 0)

    # ---- 2) 当日门槛名单（榜单/跌出榜单判定 + 重选候选） ----
    picked = mc.select_top(mf, as_of, int(cfg["top_x"]),
                           cfg.get("min_rps"), domain=_domain(cfg, data_dir),
                           rank_key=cfg["rank_key"])
    picked_codes = set(picked["code"].to_list()) if picked.height else set()
    by_code = {r["code"]: r for r in picked.to_dicts()}

    # ---- 3) 空仓重选判定 ----
    messages: list[str] = []
    signals: list[dict] = []
    idle_days = 0
    idle_start = pool_state.get("idle_start")
    if not pos_codes:
        if idle_start is None:
            idle_start = as_of
        idle_days = len([d for d in mf.calendar
                         if (idle_start or as_of) <= d <= as_of])
    rebalanced = False
    new_pool = pool
    if not pos_codes:
        if gate_state:
            messages.append(f"池级开关：停开仓中（健康度 {health}，"
                            f"恢复线 {enter_th * 2:.2f}）——今日不建仓")
        elif idle_days >= int(cfg["auto_idle_days"]) or not pool_codes:
            if picked.height == 0:
                messages.append(f"候选域内无票过门槛（基准日 {as_of}）——空仓等待")
                idle_start = idle_start or as_of
            else:
                new_pool = [{"code": r["code"],
                             "name": name_map.get(r["code"], r["code"])}
                            for r in picked.to_dicts()]
                idle_start = None
                rebalanced = True
                messages.append(f"动态重选（基准日 {as_of}）：新池 {picked.height} 只")
                for r in picked.to_dicts():
                    amount = round(float(cfg["initial_capital"])
                                   * float(cfg["suggest_pct"]), 0)
                    ref = daily_close.get(r["code"])
                    sid = db.add_live_signal(
                        "premarket", "开仓", r["code"],
                        name_map.get(r["code"], r["code"]),
                        f"动态重选入池（{cfg['rank_key']}排序）",
                        amount, ref,
                        extra={"as_of": as_of, "rps": r.get("rps"),
                               "pool_size": picked.height})
                    signals.append({"id": sid, "code": r["code"],
                                    "stype": "开仓",
                                    "name": name_map.get(r["code"], r["code"]),
                                    "reason": f"动态重选入池（{cfg['rank_key']}排序）",
                                    "suggest_amount": amount, "ref_price": ref})
        else:
            messages.append(f"空仓第 {idle_days} 日（重选阈值 "
                            f"{cfg['auto_idle_days']}）——继续等待")
    elif rebalanced is False and pool_codes:
        messages.append(f"当前池 {len(pool_codes)} 只，持仓 {len(pos_codes)} 只——未触发重选")

    # ---- 4) 持仓票退出检查（exit_need 信号数） ----
    warns: list[dict] = []
    for p in positions:
        code = p["code"]
        row = (mf.feats.filter((pl.col("code") == code)
                               & (pl.col("day") == as_of)))
        if row.height == 0:
            continue
        r = row.to_dicts()[0]
        hits = []
        if r.get("macd_ok") is False:
            hits.append("MACD死叉")
        if r.get("above") is False:
            hits.append(f"跌破MA{int(cfg['above_ma'])}")
        if r.get("score") is not None and r.get("score") < 0:
            hits.append("动量转负")
        if code not in picked_codes:
            hits.append("跌出榜单")
        if len(hits) >= int(cfg["exit_need"]):
            reason = "衰退预警(" + "+".join(hits) + f"，{len(hits)}/{cfg['exit_need']})"
            sid = db.add_live_signal(
                "premarket", "清仓", code, p.get("name") or code, reason,
                None, daily_close.get(code),
                extra={"as_of": as_of, "hits": hits})
            warns.append({"id": sid, "code": code, "stype": "清仓",
                          "name": p.get("name") or code, "reason": reason})
            messages.append(f"⚠ {code} {p.get('name') or ''}：{reason}——建议清仓")
        else:
            messages.append(f"✓ {code}：持仓正常（{'/'.join(hits) if hits else '无衰退信号'}）")

    # ---- 5) 组装推送 + 落库 ----
    if not pos_codes and not signals and gate_state == 0:
        # 无持仓且当日无重选：把建仓名单也推出去（用户可提前挂单）
        for r in list(by_code.values())[:int(cfg["pool_n"])]:
            pass  # M1：名单在池子消息里展示即可，不逐票产生开仓信号
    header = (f"【盘前信号 {as_of}】\n"
              + (f"⚠ 数据截至 {as_of}（滞后 {stale_days} 天）——选股基于不完整数据，"
                 f"请先在数据管理页更新日线\n" if stale else "")
              + f"池级 gate：{'停开仓' if gate_state else '正常'}"
              f"（健康度 {health}）\n"
              f"虚拟持仓：{len(pos_codes)} 只｜空仓 {idle_days} 日\n"
              + ("\n".join(messages) if messages else "无变化"))
    if picked.height:
        names = ", ".join(
            f"{r['code']}" for r in picked.to_dicts()[:10])
        header += f"\n当日门槛名单（前10/{picked.height}）：{names}"

    pushed = feishu.send_text(header) if push else False
    db.add_live_signal("premarket", "池子", None, "", header[:500],
                       None, None, status="信息",
                       extra={"health": health, "gate_state": gate_state,
                              "rebalanced": rebalanced})
    db.save_live_pool(new_pool, as_of, gate_state, history, idle_start)

    return {"as_of": as_of, "health": health, "gate_state": gate_state,
            "gate_changed": gate_state != int(pool_state.get("gate_state") or 0),
            "rebalanced": rebalanced, "pool": new_pool,
            "positions": len(pos_codes), "idle_days": idle_days,
            "stale": stale, "stale_days": stale_days,
            "signals": signals, "warns": warns,
            "message": header, "pushed": pushed}


def _domain(cfg: dict, data_dir) -> Optional[set]:
    """候选域（复用回测 _auto_domain：指数并集 ∩ 板块并集，均空=全市场）"""
    if not cfg.get("auto_index") and not cfg.get("auto_boards"):
        return None
    return _auto_domain(cfg, data_dir)
