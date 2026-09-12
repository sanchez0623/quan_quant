# -*- coding: utf-8 -*-
"""实锤：同池同期（S15 段），动态路径 vs 静态路径的 fwd_t 触发数对比。"""
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

for tag, auto in (("动态(auto)+分钟", True), ("静态(同池)+分钟", False)):
    cfg = base_cfg()
    cfg["period"] = "minute5"
    cfg["universe_auto"] = auto
    cfg["universe"] = [] if auto else s15["universe"]
    cfg["auto_index"] = ["zz500"] if auto else []
    cfg["start_date"] = s15["start"]
    cfg["end_date"] = s15["end"]
    cfg["params"]["fwd_t"] = "on"
    cfg["name"] = f"chk_ab_{tag}"
    rep = runner.run_backtest(cfg)
    tl = rep.get("trade_log") or []
    fwd_cnt = sum(1 for t in tl if "正向T" in str(t.get("reason", "")))
    grid_up = sum(1 for t in tl if "升破上网格线" in str(t.get("reason", "")))
    m = rep.get("metrics") or {}
    print(f"{tag}: 交易 {len(tl)} 笔｜正向T {fwd_cnt} 笔｜网格高抛 {grid_up} 笔｜"
          f"收益 {m.get('total_return'):+.2%}", flush=True)
