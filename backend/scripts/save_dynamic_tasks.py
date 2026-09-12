# -*- coding: utf-8 -*-
"""动态语境终局落库：B 基座 / HALF_GATE（消融最佳）/ D-寻优最优（未过采纳线，复查用）。

数据源：
- D-寻优最优 overrides = stageD_opt_*.md 报告的 JSON 块（唯一事实源）
- HALF_GATE overrides = stage2._load_best_overrides()（--auto 联动 D 版名单）
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv.append("--auto")  # stage2/stage3 模块级 AUTO 开关联动（D 版名单 + stageD 缓存）

from scripts.stage0_anchors import END_DEFAULT, START_DEFAULT, _cfg  # noqa: E402
from scripts.stage3_optimize import OUT_DIR, _apply, _load_half_gate_overrides  # noqa: E402

from app import db  # noqa: E402
from app.engine import runner  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "data" / "reports"
OOS_SPLIT = "2025-07-03"  # D-消融/D-寻优一致的全区间 70% 分位切分日


def load_final_ov() -> dict:
    """从最新的 stageD_opt_*.md 报告读寻优最优配置（转 tuple 键）。"""
    md = sorted(OUT_DIR.glob("stageD_opt_*.md"))[-1]
    block = md.read_text(encoding="utf-8").split("```json")[1].split("```")[0]
    print(f"寻优最优来源：{md.name}", flush=True)
    return {tuple(k.split(".", 1)): v for k, v in json.loads(block).items()}


def run_and_save(name: str, cfg: dict) -> None:
    rep = runner.run_backtest(cfg)
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, name, "backtest", payload)
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    m = rep.get("metrics") or {}
    print(f"{tid}  {name}  收益 {m.get('total_return'):+.2%}  "
          f"超额 {m.get('excess_return'):+.2%}", flush=True)


def dyn_cfg(name: str, start: str, end: str) -> dict:
    return _cfg(name, [], universe_auto=True, start=start, end=end, capital=3_000_000.0)


def main():
    REPORTS.mkdir(exist_ok=True)
    final_ov = load_final_ov()
    half_gate = _load_half_gate_overrides()

    run_and_save("D语境-B基座(动态L2)-全区间", dyn_cfg("D_B_base", START_DEFAULT, END_DEFAULT))
    run_and_save("D语境-HALF_GATE(消融最佳)-全区间",
                 _apply(dyn_cfg("D_half_gate", START_DEFAULT, END_DEFAULT), half_gate))
    run_and_save("D语境-阶段3寻优最优-全区间(未过采纳线)",
                 _apply(dyn_cfg("D_stage3_best", START_DEFAULT, END_DEFAULT), final_ov))
    run_and_save("D语境-阶段3寻优最优-OOS段(未过采纳线)",
                 _apply(dyn_cfg("D_stage3_best_oos", OOS_SPLIT, END_DEFAULT), final_ov))


if __name__ == "__main__":
    main()
