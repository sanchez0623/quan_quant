# -*- coding: utf-8 -*-
"""龙头低吸（dragon_dip）OAT 单因素实验批。

归因依据（bt_71d13247d3d7 长窗口 2022-09-01~2026-09-24 基线 -90.61%）：
- 止损路径 69 回合实际均值 -9.1%（名义 5%），亏损 ≈ 全部净亏 -> E1 放宽止损
- 竞价低吸 96 回合 -42.4 万（最弱买点）            -> E2 纯炸板对照
- euphoria 缩仓覆盖 39.6% 天数（max_boards 中位 5）-> E3 高潮门槛抬到 8 板
- 首阴 4 年仅 10 次触发（min_boards=3 过严）        -> E4 放宽到 2 板
- 单票 base_pct=50% × 2 槽满仓两只票，回撤放大器   -> E5 降到 30%

每实验独立落库三件套（create_task + save_report + update_task），
中间实验不打 tag；采纳形态由用户拍板后补 tag。
用法：python scripts/_dragon_dip_oat.py
"""
import copy
import json
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

from _dragon_dip_attribution import rounds

from app import db
from app.engine import runner
from _dragon_dip_baseline import build_cfg

START, END = "2022-09-01", "2026-09-24"

EXPERIMENTS = [
    # (名称, params 覆盖, risk_config 覆盖)
    ("OAT-E1a 止损8%", {"stop_loss_pct": 8.0}, {}),
    ("OAT-E1b 止损10%", {"stop_loss_pct": 10.0}, {}),
    ("OAT-E1c 止损12%", {"stop_loss_pct": 12.0}, {}),
    ("OAT-E2 纯炸板", {"entry_type": "zha"}, {}),
    ("OAT-E3 高潮门槛8板", {"euphoria_boards": 8}, {}),
    ("OAT-E4 首阴2板起", {"min_boards": 2}, {}),
    ("OAT-E5 单票30%", {"base_pct": 30.0}, {}),
]


def summarize(rep: dict) -> str:
    rs = [r for r in rounds(rep) if r["exit"] not in ("未平仓", "减仓未平")]
    stops = [r for r in rs if r["exit"] == "止损"]
    tps = [r for r in rs if r["exit"] == "止盈"]
    gap = [r for r in rs if r["entry"] == "竞价低吸"]
    zha = [r for r in rs if r["entry"] == "炸板低吸"]

    def _sum(v):
        return sum(r["pnl"] for r in v)

    stop_avg = (_sum(stops) / sum(r["open_amt"] for r in stops)
                if stops else 0.0)
    return (f"回合{len(rs)} 止损{len(stops)}笔(均值{stop_avg:+.1%},"
            f"{_sum(stops):+,.0f}) 止盈{len(tps)}({_sum(tps):+,.0f}) "
            f"竞价{_sum(gap):+,.0f} 炸板{_sum(zha):+,.0f}")


def main() -> None:
    reports = BACKEND.parent / "data" / "reports"
    rows = []
    for name, p_over, r_over in EXPERIMENTS:
        cfg = build_cfg()
        cfg["name"] = f"龙头低吸 {name}-2022-09长窗口"
        cfg["start_date"] = START
        cfg["end_date"] = END
        cfg["params"] = dict(p_over)
        risk = copy.deepcopy(cfg.get("risk_config") or {})
        risk.update(r_over)
        if "stop_loss_pct" in p_over:
            risk["stop_loss_pct"] = p_over["stop_loss_pct"]
        cfg["risk_config"] = risk
        print(f"\n>>> {name} params={p_over} risk_over={r_over}", flush=True)
        rep = runner.run_backtest(cfg)
        tid = "bt_" + uuid.uuid4().hex[:12]
        path = reports / f"{tid}.json"
        path.write_text(json.dumps(rep, ensure_ascii=False, default=str),
                        encoding="utf-8")
        payload = {"strategy_id": cfg["strategy_id"], "period": cfg["period"],
                   "config": cfg, "report_path": str(path)}
        db.create_task(tid, cfg["name"], "backtest", payload)
        db.save_report(tid, str(path))
        db.update_task(tid, status="success", progress=100, message="")
        m = rep.get("metrics") or {}
        row = {"name": name, "tid": tid,
               "ret": m.get("total_return"), "mdd": m.get("max_drawdown"),
               "sharpe": m.get("sharpe"), "n": m.get("total_trades")}
        rows.append(row)
        print(f"    {tid} 收益 {row['ret']:+.2%} 回撤 {row['mdd']:+.2%} "
              f"夏普 {row['sharpe']:.2f} | {summarize(rep)}", flush=True)

    print("\n===== OAT 汇总（基线 -90.61% / MDD -91.44%）=====", flush=True)
    for r in rows:
        print(f"{r['name']:<18} {r['tid']}  收益 {r['ret']:+.2%}  "
              f"回撤 {r['mdd']:+.2%}  夏普 {r['sharpe']:.2f}  交易 {r['n']}",
              flush=True)


if __name__ == "__main__":
    main()
