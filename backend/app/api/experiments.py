# -*- coding: utf-8 -*-
"""对比实验（TREN_T_COMPARISON）接口：2×2 矩阵（时钟×T）× 资金档，
后端编排子回测任务 + 归因分解（T 边际 / 时钟效应 / 交互项）。
"""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import config, db
from ..auth import get_current_user
from ..task_manager import manager
from .backtests import validate_backtest_config

router = APIRouter(prefix="/api/experiments", tags=["experiments"])

# 实验矩阵：cell -> 覆盖参数。
# T 开格（A/B）只设 trend_clock，max_t_times 读基座配置（默认 4），不覆盖用户自定义；
# T 关格（C/D）强制 max_t_times=0。
CELLS = {
    "A": {"trend_clock": "daily"},                      # 日线时钟 × T（基座 max_t_times）
    "B": {"trend_clock": "intraday"},                   # 盘中时钟 × T（现状）
    "C": {"trend_clock": "daily", "max_t_times": 0},    # 日线时钟 × 无T
    "D": {"trend_clock": "intraday", "max_t_times": 0},  # 盘中时钟 × 无T
}
CELL_LABELS = {"A": "日线时钟×T", "B": "盘中时钟×T",
               "C": "日线时钟×无T", "D": "盘中时钟×无T", "E": "纯日线15年(参考)"}
# E 格：纯日线、做T硬关、2010 起 15 年窗口的趋势层稳健性参考（不进 2×2 归因）
E_START = "2010-01-01"

# T_REFACTOR 机制竞争矩阵（L3）：t_mode 四机制（A/B/C/D）
TMODE_CELLS = {
    "A": {"t_mode": "grid"},         # L1 修好的网格（双止损）
    "B": {"t_mode": "discipline"},   # L2 回补纪律
    "C": {"t_mode": "off"},          # 无T基线
    "D": {"t_mode": "time"},         # 时点规律T
}
TMODE_LABELS = {"A": "网格+双止损(L1)", "B": "回补纪律(L2)",
                "C": "无T(C)", "D": "时点规律T(D)"}
TMODE_METRICS = ("total_return", "sharpe", "max_drawdown", "t_pnl", "t_pnl_closed", "commission_total")

# momentum_slot 2×2：正向T × 债务时限（两轴均为独立真旋钮，
# 回答：正向T是否"逆势接飞刀"的负贡献 + 做T债务时限怎么设）
FWD_T_DEBT_CELLS = {
    "A": {"fwd_t": "off", "t_debt_max_days": 3},    # 关正向T × 短债务（推荐）
    "B": {"fwd_t": "on", "t_debt_max_days": 3},     # 开正向T × 短债务
    "C": {"fwd_t": "off", "t_debt_max_days": 10},   # 关正向T × 长债务
    "D": {"fwd_t": "on", "t_debt_max_days": 10},    # 开正向T × 长债务
}
FWD_T_DEBT_LABELS = {"A": "关正T×债3", "B": "开正T×债3",
                     "C": "关正T×债10", "D": "开正T×债10"}

# 矩阵注册表：matrix -> 格子覆盖、标签、支持策略、是否支持E格（纯日线15年参考）
MATRIX_DEFS = {
    "clock": {"cells": CELLS, "labels": CELL_LABELS,
              "strategies": ("momentum_t",), "with_e": True},
    "t_mode": {"cells": TMODE_CELLS, "labels": TMODE_LABELS,
               "strategies": ("momentum_t", "momentum_slot"), "with_e": False},
    "fwd_t_debt": {"cells": FWD_T_DEBT_CELLS, "labels": FWD_T_DEBT_LABELS,
                   "strategies": ("momentum_slot",), "with_e": False},
}

# 2×2 归因决策文案（按矩阵配置轴名，通用化）
_DECISION_WORDS = {
    "clock": {"a1": "T开关", "a2": "盘中时钟(相对日线)",
              "a1_neg_advice": "→ 可考虑砍掉做T层（参数少、过拟合风险降）",
              "a2_pos_advice": "→ 保留5分钟架构",
              "a2_neg_advice": "→ 趋势层可降级日线"},
    "fwd_t_debt": {"a1": "债务时限(3天相对10天)", "a2": "正向T(off相对on)",
                   "a1_neg_advice": "→ 债务时限影响小，取默认3即可",
                   "a2_pos_advice": "→ 正向T有正贡献，可考虑开启",
                   "a2_neg_advice": "→ 建议 fwd_t=off（正向T在趋势下行易接飞刀）"},
}


class ExperimentRequest(BaseModel):
    name: str = "对比实验"
    base_config: dict
    cells: list[str] = Field(default_factory=lambda: ["A", "B", "C", "D"])
    capitals: list[float] = Field(default_factory=lambda: [400_000, 3_000_000])
    start_date: str
    end_date: str
    with_e: bool = False  # 是否附带 E 格（纯日线 15 年参考，默认关）
    matrix: str = "clock"  # clock=趋势×T 2x2 / t_mode=四机制竞争（L3）


def _metrics_for(task_id: str) -> dict | None:
    path = db.get_report_path(task_id)
    if not path or not Path(path).exists():
        return None
    try:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
        return r.get("metrics") or {}
    except Exception:  # noqa: BLE001
        return None


# 归因覆盖的指标（文档 §2.4：total_return/sharpe/max_drawdown/t_pnl/commission_total）
_ATTRIB_METRICS = ("total_return", "sharpe", "max_drawdown", "t_pnl", "commission_total")


def _deltas_for(m: dict, metric: str) -> dict:
    """对单个资金档、单个指标做 2×2 差值分解"""
    def g(cell: str):
        mm = m.get(cell) or {}
        v = mm.get(metric)
        return float(v) if isinstance(v, (int, float)) else None

    t_ac = (g("A") - g("C")) if (g("A") is not None and g("C") is not None) else None
    t_bd = (g("B") - g("D")) if (g("B") is not None and g("D") is not None) else None
    c_ab = (g("A") - g("B")) if (g("A") is not None and g("B") is not None) else None
    c_cd = (g("C") - g("D")) if (g("C") is not None and g("D") is not None) else None
    inter = (t_ac - t_bd) if (t_ac is not None and t_bd is not None) else None
    return {"t_margin_ac": t_ac, "t_margin_bd": t_bd,
            "clock_ab": c_ab, "clock_cd": c_cd, "interaction": inter}


def _attribution_for(m: dict) -> dict:
    """对单个资金档做归因分解。m: cell -> metrics dict。
    顶层 t_margin_ac 等为 total_return 的主归因；`metrics` 给出各指标差值分解。"""
    def _sig(x):
        return None if x is None else (1 if x > 1e-9 else (-1 if x < -1e-9 else 0))

    metrics = {k: _deltas_for(m, k) for k in _ATTRIB_METRICS}
    tr = metrics["total_return"]
    return {
        "cells": {c: m.get(c) for c in m},
        "t_margin_ac": tr["t_margin_ac"], "t_margin_bd": tr["t_margin_bd"],
        "clock_ab": tr["clock_ab"], "clock_cd": tr["clock_cd"],
        "interaction": tr["interaction"],
        "t_consistent": (_sig(tr["t_margin_ac"]) == _sig(tr["t_margin_bd"])
                         if (tr["t_margin_ac"] is not None and tr["t_margin_bd"] is not None)
                         else None),
        "clock_consistent": (_sig(tr["clock_ab"]) == _sig(tr["clock_cd"])
                             if (tr["clock_ab"] is not None and tr["clock_cd"] is not None)
                             else None),
        "metrics": metrics,
    }


def _decision(per_capital: dict, words: dict) -> str:
    """2×2 归因决策（通用）：基于各资金档归因给出方向性结论。
    words: _DECISION_WORDS[matrix]，提供轴名 a1(列 A−C/B−D) 与 a2(行 A−B/C−D) 及建议文案。"""
    t_ac_vals = [a["t_margin_ac"] for a in per_capital.values() if a["t_margin_ac"] is not None]
    t_bd_vals = [a["t_margin_bd"] for a in per_capital.values() if a["t_margin_bd"] is not None]
    c_ab_vals = [a["clock_ab"] for a in per_capital.values() if a["clock_ab"] is not None]
    c_cd_vals = [a["clock_cd"] for a in per_capital.values() if a["clock_cd"] is not None]
    t_con = [a["t_consistent"] for a in per_capital.values() if a["t_consistent"] is not None]
    a1, a2 = words["a1"], words["a2"]
    lines = []

    def _avg(vs):
        return sum(vs) / len(vs) if vs else None

    t_ac_avg, t_bd_avg = _avg(t_ac_vals), _avg(t_bd_vals)
    c_ab_avg, c_cd_avg = _avg(c_ab_vals), _avg(c_cd_vals)

    # 跨资金档翻转 → 路径依赖
    def _flip(vs):
        return vs and (max(vs) > 1e-9) and (min(vs) < -1e-9)
    if _flip(t_ac_vals) or _flip(t_bd_vals) or _flip(c_ab_vals) or _flip(c_cd_vals):
        lines.append("⚠ 结论跨资金档翻转 → 路径依赖，各格结论降级为不可采信")
    if t_ac_avg is not None and t_bd_avg is not None:
        if not (t_con and all(t_con)):
            lines.append(f"{a1}×{a2}交互显著：两行{a1}估计方向不一致，结论须分别报告")
        elif t_ac_avg <= 0 and t_bd_avg <= 0:
            lines.append(f"{a1} 无净价值（A−C≈B−D≈0或负）{words.get('a1_neg_advice', '')}".rstrip())
        else:
            lines.append(f"{a1} 有真实增强（A−C={t_ac_avg:+.1%}，B−D={t_bd_avg:+.1%}）")
    if c_ab_avg is not None and c_cd_avg is not None:
        if c_ab_avg > 0 and c_cd_avg > 0:
            lines.append(f"{a2} 有正贡献（A−B={c_ab_avg:+.1%}，C−D={c_cd_avg:+.1%}）"
                         f"{words.get('a2_pos_advice', '')}".rstrip())
        elif c_ab_avg <= 0 and c_cd_avg <= 0:
            lines.append(f"{a2} 无价值或负贡献（A−B={c_ab_avg:+.1%}，C−D={c_cd_avg:+.1%}）"
                         f"{words.get('a2_neg_advice', '')}".rstrip())
        else:
            lines.append(f"{a2} 两行方向不一致 → 分别报告")
    return "；".join(lines) if lines else "数据不足，无法给出归因结论"


def _tmode_attribution_for(m: dict) -> dict:
    "t_mode 机制矩阵归因：4 机制两两差值（B-A/A-C/D-C/D-A）+ 各指标分解"
    def _g(cell, key):
        mm = m.get(cell) or {}
        v = mm.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def _diff(x, y):
        return (x - y) if (x is not None and y is not None) else None

    cells = {c: m.get(c) for c in m}
    metrics = {}
    for k in TMODE_METRICS:
        metrics[k] = {
            "grid": _g("A", k), "discipline": _g("B", k),
            "off": _g("C", k), "time": _g("D", k),
            "discipline_vs_grid": _diff(_g("B", k), _g("A", k)),
            "grid_vs_off": _diff(_g("A", k), _g("C", k)),
            "time_vs_off": _diff(_g("D", k), _g("C", k)),
            "time_vs_grid": _diff(_g("D", k), _g("A", k)),
        }
    return {
        "cells": cells, "metrics": metrics,
        "discipline_vs_grid": _diff(_g("B", "total_return"), _g("A", "total_return")),
        "grid_vs_off": _diff(_g("A", "total_return"), _g("C", "total_return")),
        "time_vs_off": _diff(_g("D", "total_return"), _g("C", "total_return")),
        "time_vs_grid": _diff(_g("D", "total_return"), _g("A", "total_return")),
    }


def _tmode_decision(per_capital: dict) -> str:
    "t_mode 机制竞争决策：跨资金档取均值比较 + 综合最优"
    from collections import defaultdict
    lines = []

    def _avg(key):
        vals = [a.get(key) for a in per_capital.values() if a.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    dg = _avg("discipline_vs_grid")
    go = _avg("grid_vs_off")
    to = _avg("time_vs_off")
    tg = _avg("time_vs_grid")
    if dg is not None:
        lines.append(f"回补纪律 vs 网格：{dg:+.2%}" + ("，纪律更优" if dg > 1e-9 else ("，网格更优" if dg < -1e-9 else "，无差异")))
    if go is not None:
        lines.append(f"网格 vs 无T：{go:+.2%}" + ("，T层有净价值" if go > 1e-9 else ("，T层无净价值" if go < -1e-9 else "，无差异")))
    if to is not None:
        lines.append(f"时点T vs 无T：{to:+.2%}" + ("，时点规律有净价值" if to > 1e-9 else ("，无净价值" if to < -1e-9 else "，无差异")))
    if tg is not None:
        lines.append(f"时点T vs 智能网格：{tg:+.2%}" + ("，无脑时点胜过智能网格（重要证伪）" if tg > 1e-9 else ""))
    agg = defaultdict(list)
    for a in per_capital.values():
        for cell in ("A", "B", "C", "D"):
            v = (a.get("cells") or {}).get(cell, {}).get("total_return")
            if isinstance(v, (int, float)):
                agg[cell].append(float(v))
    if agg:
        best = max(agg, key=lambda c: sum(agg[c]) / len(agg[c]))
        lines.append(f"综合最优：{TMODE_LABELS[best]}")
    return "；".join(lines) if lines else "数据不足，无法给出归因结论"


@router.post("")
def create_experiment(req: ExperimentRequest, _user: str = Depends(get_current_user)):
    if req.matrix not in MATRIX_DEFS:
        raise HTTPException(status_code=400, detail=f"matrix 需为 {list(MATRIX_DEFS)} 之一，非法: {req.matrix}")
    mdef = MATRIX_DEFS[req.matrix]
    cell_defs, labels = mdef["cells"], mdef["labels"]
    sid = req.base_config.get("strategy_id")
    if sid not in mdef["strategies"]:
        raise HTTPException(status_code=400,
                            detail=f"矩阵 {req.matrix} 不支持策略 {sid}，仅支持: {'/'.join(mdef['strategies'])}")
    bad = [c for c in req.cells if c not in cell_defs]
    if bad:
        raise HTTPException(status_code=400, detail=f"cells 需为 {list(cell_defs)} 的子集，非法: {bad}")
    if not req.cells or not req.capitals:
        raise HTTPException(status_code=400, detail="cells 与 capitals 不能为空")
    exp_id = "exp_" + uuid.uuid4().hex[:12]
    sub_ids: list[str] = []
    for cell in req.cells:
        for cap in req.capitals:
            cfg = dict(req.base_config)
            cfg["name"] = f"{req.name} · {labels[cell]} · {int(cap)}"
            cfg["start_date"] = req.start_date
            cfg["end_date"] = req.end_date
            cfg["initial_capital"] = float(cap)
            params = dict(cfg.get("params") or {})
            params.update(cell_defs[cell])
            cfg["params"] = params
            cfg = validate_backtest_config(cfg)
            tid = "bt_" + uuid.uuid4().hex[:12]
            db.create_task(tid, cfg["name"], "backtest",
                           payload={"strategy_id": "momentum_t", "period": cfg["period"],
                                    "config": cfg, "experiment_id": exp_id,
                                    "cell": cell, "capital": float(cap)})
            manager.submit("backtest", tid, backtest_config=cfg)
            sub_ids.append(tid)
    stored_cells = list(req.cells)
    # E 格（纯日线 15 年参考）：仅 clock 矩阵支持，单独开关，不进 2×2 归因
    if req.with_e and mdef["with_e"]:
        e_cfg = dict(req.base_config)
        e_cfg["name"] = f"{req.name} · {CELL_LABELS['E']} · {int(req.capitals[0])}"
        e_cfg["start_date"] = E_START
        e_cfg["end_date"] = req.end_date
        e_cfg["period"] = "daily"
        e_cfg["initial_capital"] = float(req.capitals[0])
        e_params = dict(e_cfg.get("params") or {})
        e_params.update({"trend_clock": "daily", "max_t_times": 0})
        e_cfg["params"] = e_params
        e_cfg = validate_backtest_config(e_cfg)
        etid = "bt_" + uuid.uuid4().hex[:12]
        db.create_task(etid, e_cfg["name"], "backtest",
                       payload={"strategy_id": "momentum_t", "period": "daily",
                                "config": e_cfg, "experiment_id": exp_id,
                                "cell": "E", "capital": float(req.capitals[0])})
        manager.submit("backtest", etid, backtest_config=e_cfg)
        sub_ids.append(etid)
        stored_cells.append("E")
    db.create_experiment(exp_id, req.name, dict(req.base_config), stored_cells,
                         req.capitals, sub_ids, req.start_date, req.end_date,
                         matrix=req.matrix)
    return {"experiment_id": exp_id, "sub_task_ids": sub_ids, "status": "pending"}


@router.get("")
def list_experiments(_user: str = Depends(get_current_user)):
    out = []
    for e in db.list_experiments():
        subs = db.list_tasks("backtest")
        by_id = {t["task_id"]: t for t in subs}
        done = sum(1 for tid in e["sub_task_ids"]
                   if by_id.get(tid) and by_id[tid]["status"] in ("success", "failed"))
        total = len(e["sub_task_ids"]) or 1
        # 状态语义：全部子任务完成时，有任一失败 -> failed，否则 success；未完成 -> running
        if done < len(e["sub_task_ids"]):
            status = "running"
        else:
            failed_ids = [tid for tid in e["sub_task_ids"]
                          if by_id.get(tid) and by_id[tid]["status"] == "failed"]
            status = "failed" if failed_ids else "success"
        err = e["error"]
        if status == "failed" and not err:
            err = next((by_id[tid].get("error") or "子任务失败" for tid in failed_ids), "子任务失败")
        out.append({
            "experiment_id": e["experiment_id"], "name": e["name"],
            "cells": e["cells"], "capitals": e["capitals"],
            "matrix_type": e.get("matrix") or "clock",
            "status": status, "progress": round(done / total * 100, 1),
            "error": err, "created_at": e["created_at"],
            "finished_at": e["finished_at"], "sub_count": len(e["sub_task_ids"]),
            "sub_task_ids": e["sub_task_ids"],
        })
    return out


@router.get("/{exp_id}")
def get_experiment(exp_id: str, _user: str = Depends(get_current_user)):
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="实验不存在")
    # 矩阵：每个子任务的实时状态 + 已完成格子的指标摘要
    matrix, per_capital = [], {}
    done = 0
    for tid in exp["sub_task_ids"]:
        t = db.get_task(tid)
        if not t:
            continue
        pl = t.get("payload") or {}
        cell = pl.get("cell", "")
        cap = float(pl.get("capital", 0))
        m = _metrics_for(tid) if t["status"] == "success" else None
        if t["status"] in ("success", "failed"):
            done += 1
        if m:
            per_capital.setdefault(str(int(cap)), {})[cell] = m
        matrix.append({
            "task_id": tid, "cell": cell, "capital": cap,
            "status": t["status"], "progress": t["progress"], "message": t["message"],
            "error": t["error"], "metrics": m,
        })
    matrix_key = exp.get("matrix") or "clock"
    if matrix_key == "t_mode":
        attr_per_capital = {k: _tmode_attribution_for(v) for k, v in per_capital.items()}
        decision = _tmode_decision(attr_per_capital) if attr_per_capital else "数据不足，无法给出归因结论"
    else:
        attr_per_capital = {k: _attribution_for(v) for k, v in per_capital.items()}
        words = _DECISION_WORDS.get(matrix_key, _DECISION_WORDS["clock"])
        decision = _decision(attr_per_capital, words) if attr_per_capital else "数据不足，无法给出归因结论"
    attribution = {
        "per_capital": attr_per_capital,
        "decision": decision,
    }
    total = len(exp["sub_task_ids"]) or 1
    # 与列表一致的状态语义：有任一失败 -> failed，全部成功 -> success，未完成 -> running
    if done < len(exp["sub_task_ids"]):
        status = "running"
    else:
        failed_ids = [tid for tid in exp["sub_task_ids"]
                      if db.get_task(tid) and db.get_task(tid)["status"] == "failed"]
        status = "failed" if failed_ids else "success"
    err = exp["error"]
    if status == "failed" and not err:
        err = next((db.get_task(tid)["error"] or "子任务失败" for tid in failed_ids), "子任务失败")
    return {
        "experiment_id": exp["experiment_id"], "name": exp["name"],
        "base_config": exp["base_config"], "cells": exp["cells"],
        "capitals": exp["capitals"], "start_date": exp["start_date"],
        "end_date": exp["end_date"],
        "status": status,
        "progress": round(done / total * 100, 1),
        "error": err, "created_at": exp["created_at"],
        "finished_at": exp["finished_at"],
        "sub_task_ids": exp["sub_task_ids"],
        "matrix_type": exp.get("matrix") or "clock",
        "matrix": matrix, "attribution": attribution,
    }


@router.delete("/{exp_id}")
def delete_experiment(exp_id: str, _user: str = Depends(get_current_user)):
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="实验不存在")
    for tid in exp.get("sub_task_ids") or []:
        db.delete_task(tid)
    db.delete_experiment(exp_id)
    return {"ok": True}
