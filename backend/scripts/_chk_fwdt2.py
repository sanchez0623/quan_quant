# -*- coding: utf-8 -*-
"""定位 fwd_t 0 触发层：单段静态分钟回测（绕开动态分段）vs 动态分段，数正向T。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import base_cfg  # noqa: E402
from app.engine import runner  # noqa: E402

rep_all = json.loads((Path(__file__).resolve().parents[2] / "data" / "reports" /
                      "bt_124c6d43225a.json").read_text(encoding="utf-8"))
s15 = next(s for s in rep_all["auto_segments"] if s["seg"] == 15)

cfg = base_cfg()
cfg["period"] = "minute5"
cfg["universe_auto"] = False
cfg["universe"] = s15["universe"]
cfg["auto_index"] = []
cfg["start_date"] = s15["start"]
cfg["end_date"] = s15["end"]
cfg["params"]["fwd_t"] = "on"
cfg["name"] = "chk_fwdt_static_s15"

rep = runner.run_backtest(cfg)
tl = rep.get("trade_log") or []
reasons = Counter(str(t.get("reason", ""))[:16] for t in tl)
print(f"单段静态分钟（S15 池 30 只，{s15['start']}~{s15['end']}）：")
print(f"  交易 {len(tl)} 笔")
print("  reason 分布：")
for r, n in reasons.most_common(12):
    print(f"    {n:>4}  {r}")
gd = rep.get("gate_days") or {}
if gd:
    n_true = sum(1 for v in gd.values() if v)
    print(f"  gate_days: {n_true}/{len(gd)} 日 gate 触发")
else:
    print("  gate_days: 报告无此字段")
