# -*- coding: utf-8 -*-
"""验证动态+分钟路径下做T层是否生效：对比 BASE_M5 / FWD_T 的做T交易。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_fwdt import FWD, mk5  # noqa: E402
from app.engine import runner  # noqa: E402

for tag, ov in (("BASE_M5", {}), ("FWD_T", FWD)):
    cfg = mk5(ov, False, f"chk_{tag}")
    rep = runner.run_backtest(cfg)
    tl = rep.get("trade_log") or []
    tcnt = Counter(str(t.get("reason", ""))[:12] for t in tl if "做T" in str(t.get("tag", "")))
    tags = Counter(t.get("tag") for t in tl)
    m = rep.get("metrics") or {}
    print(f"{tag}: 交易 {len(tl)} 笔，tag 分布 {dict(tags)}")
    print(f"  做T reason 前5: {dict(tcnt.most_common(5))}")
    print(f"  收益 {m.get('total_return'):+.2%} 超额 {m.get('excess_return'):+.2%}")
