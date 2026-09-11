# -*- coding: utf-8 -*-
"""L6 防御验证：bt_e2b0ca48c46d 原配置重跑，对比修复前后的止损结构。"""
import json
from collections import defaultdict
from pathlib import Path

sys_p = Path(__file__).resolve().parents[1]
sys_path = str(sys_p)
import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from app.engine import runner  # noqa: E402

TASK = "bt_e2b0ca48c46d"
rep_old = json.loads((Path(__file__).resolve().parents[2] / "data" / "reports" /
                      f"{TASK}.json").read_text(encoding="utf-8"))
cfg = rep_old["config"]

print("重跑同配置（L6 bad_adj 防御已激活）...", flush=True)
rep_new = runner.run_backtest(cfg)

m_old = rep_old.get("metrics") or {}
m_new = rep_new.get("metrics") or {}
for k in ("total_return", "excess_return", "max_drawdown", "sharpe", "win_rate"):
    vo, vn = m_old.get(k), m_new.get(k)
    fo = f"{vo:+.2%}" if isinstance(vo, (int, float)) else "-"
    fn = f"{vn:+.2%}" if isinstance(vn, (int, float)) else "-"
    print(f"  {k:<16} 旧 {fo:>10} | 新 {fn:>10}", flush=True)


def stop_stats(log):
    sells = [t for t in log if t.get("side") == "sell"]
    stops = [t for t in sells if "止损" in str(t.get("type") or "") + str(t.get("reason") or "")]
    pnl = sum(float(t.get("pnl") or 0) for t in stops)
    big = [t for t in stops if abs(float(t.get("pnl") or 0)) > 50000]
    return {"n": len(stops), "pnl": pnl, "big_n": len(big),
            "big_pnl": sum(float(t.get("pnl") or 0) for t in big)}


so = stop_stats(rep_old.get("trade_log") or [])
sn = stop_stats(rep_new.get("trade_log") or [])
print(f"\n止损笔数: 旧 {so['n']} | 新 {sn['n']}", flush=True)
print(f"止损合计 pnl: 旧 {so['pnl']:,.0f} | 新 {sn['pnl']:,.0f}", flush=True)
print(f"|pnl|>5万 笔数: 旧 {so['big_n']} | 新 {sn['big_n']}；金额 旧 {so['big_pnl']:,.0f} "
      f"| 新 {sn['big_pnl']:,.0f}", flush=True)

# 2023-03-28 当日交易
for tag, rep in (("旧", rep_old), ("新", rep_new)):
    day = [t for t in (rep.get("trade_log") or [])
           if str(t.get("time", "")).startswith("2023-03-28")]
    print(f"2023-03-28 当日交易: {tag} {len(day)} 笔", flush=True)

out = Path(__file__).parent / "out" / f"l6_verify_{TASK}.json"
out.write_text(json.dumps({"metrics_new": m_new, "stop_new": sn}, ensure_ascii=False,
                          default=str), encoding="utf-8")
print(f"已存 {out}", flush=True)
