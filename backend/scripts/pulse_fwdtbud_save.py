# -*- coding: utf-8 -*-
"""fwd_t_budget_pct 扫描落库补录。

规则（project_rules.md）：实验回测结论给用户复查必须三件套落库。
基线 budget=25 即 MA30 采纳形态（bt_b97b590f8d4f/bt_6e27d17bcf14 已落库），不重复。
防重 by 任务名，看门狗中断后重跑自动续。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_fwdtbud_oat import BASE_BUDGET, REPORT_GRID, mk_bud  # noqa: E402
from pulse_gatema_oat import _save  # noqa: E402
from app import config  # noqa: E402


def main():
    conn = sqlite3.connect(config.META_DB_PATH)
    names = {r[0] for r in conn.execute("SELECT name FROM tasks")}
    conn.close()
    cfg0 = base_gate_on()
    for budget in REPORT_GRID:
        if budget == BASE_BUDGET:
            continue
        for oos, tag in ((False, "全区间"), (True, "OOS段")):
            name = f"fwdT占比{budget}-{tag}(分钟)"
            if name in names:
                print(f"[跳过] {name}", flush=True)
                continue
            print(f"[落库] {name} ...", flush=True)
            _save(name, mk_bud(cfg0, budget, oos))


if __name__ == "__main__":
    main()
