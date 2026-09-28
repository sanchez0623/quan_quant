# -*- coding: utf-8 -*-
"""龙头低吸（dragon_dip）回测归因。

按回合（group_id 开仓 -> 全部卖出）分段统计盈亏：
- 买点类型（炸板低吸/首阴低吸/竞价低吸）
- 终止方式（止损/清仓/止盈/减仓未平）
- 开仓年份、买点×终止交叉
- 止损滑点（实际回合收益 vs 名义止损）
- 净值曲线最大回撤区间、月度收益分布
用法：python scripts/_dragon_dip_attribution.py <report.json>
"""
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def rounds(rep: dict) -> list[dict]:
    buys: dict = defaultdict(list)
    sells: dict = defaultdict(list)
    for t in rep.get("trade_log") or []:
        (buys if t["side"] == "buy" else sells)[t["group_id"]].append(t)
    out = []
    for gid, bs in buys.items():
        ss = sells.get(gid, [])
        b = bs[0]
        open_amt = sum(x["amount"] for x in bs) or 1.0
        realized = sum(x["pnl"] or 0.0 for x in ss)
        terminal = "未平仓"
        if ss:
            terminal = next((t for t in ("止损", "清仓", "止盈")
                             if any(s["type"] == t for s in ss)), "减仓未平")
        entry = (b.get("reason") or "").split("，")[0]
        out.append({
            "gid": gid, "code": b["code"], "entry": entry,
            "open_date": b["time"][:10],
            "close_date": ss[-1]["time"][:10] if ss else "",
            "exit": terminal, "open_amt": open_amt, "pnl": realized,
            "ret": realized / open_amt,
            "hold_days": ((date.fromisoformat(ss[-1]["time"][:10])
                           - date.fromisoformat(b["time"][:10])).days
                          if ss else None),
        })
    return out


def seg_table(rs: list[dict], keyfn) -> str:
    g: dict = defaultdict(list)
    for r in rs:
        g[keyfn(r)].append(r)
    lines = [f"{'分段':<18}{'回合':>4}{'胜率':>8}{'平均收益':>9}{'总盈亏':>12}{'均持仓天':>8}"]
    for k in sorted(g, key=str):
        v = g[k]
        n = len(v)
        wr = sum(1 for r in v if r["pnl"] > 0) / n
        avg = sum(r["ret"] for r in v) / n
        pnl = sum(r["pnl"] for r in v)
        hd = [r["hold_days"] for r in v if r["hold_days"] is not None]
        hds = f"{sum(hd) / len(hd):.1f}" if hd else "-"
        lines.append(f"{str(k):<18}{n:>4}{wr:>8.1%}{avg:>9.2%}{pnl:>12,.0f}{hds:>8}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python scripts/_dragon_dip_attribution.py <report.json>")
    path = Path(sys.argv[1])
    rep = json.loads(path.read_text(encoding="utf-8"))
    allr = rounds(rep)
    closed = [r for r in allr if r["exit"] not in ("未平仓", "减仓未平")]
    print(f"== {rep.get('name')} ==")
    print(f"开仓 {len(allr)} 回合，其中已终态平仓 {len(closed)}")
    print("\n-- 按买点类型 --")
    print(seg_table(closed, lambda r: r["entry"]))
    print("\n-- 按终止方式 --")
    print(seg_table(closed, lambda r: r["exit"]))
    print("\n-- 按开仓年份 --")
    print(seg_table(closed, lambda r: r["open_date"][:4]))
    print("\n-- 买点×终止 --")
    print(seg_table(closed, lambda r: f"{r['entry']}|{r['exit']}"))

    stops = [r for r in closed if r["exit"] == "止损"]
    if stops:
        rets = [r["ret"] for r in stops]
        print(f"\n止损回合 {len(stops)}：实际收益 均值{_pct(sum(rets) / len(rets))} "
              f"中位{_pct(statistics.median(rets))} 最差{_pct(min(rets))}；"
              f"亏损超10%的 {sum(1 for x in rets if x < -0.10)} 笔")

    worst = sorted(closed, key=lambda r: r["pnl"])[:10]
    print("\n-- 亏损 Top10 回合 --")
    for r in worst:
        print(f"  {r['open_date']} {r['code']} {r['entry']} -> {r['exit']}"
              f"{' @' + r['close_date'] if r['close_date'] else ''} "
              f"{_pct(r['ret'])} {r['pnl']:+,.0f}")

    ec = rep.get("equity_curve") or []
    peak, peak_d, mdd, mdd_pk, mdd_tr = 0.0, "", 0.0, "", ""
    for e in ec:
        v = e["adjusted_equity"]
        if v > peak:
            peak, peak_d = v, e["date"]
        dd = v / peak - 1 if peak else 0
        if dd < mdd:
            mdd, mdd_pk, mdd_tr = dd, peak_d, e["date"]
    print(f"\n最大回撤 {_pct(mdd)}（峰 {mdd_pk} -> 谷 {mdd_tr}）")

    gd = rep.get("gate_days")
    if gd:
        if isinstance(gd, dict):
            n_on = sum(1 for v in gd.values() if v)
            days_on = [k for k, v in gd.items() if v]
            head = f"（前 20: {days_on[:20]}）" if days_on else ""
            print(f"门控：共 {len(gd)} 天，gate_off=True 仅 {n_on} 天{head}")
        else:
            print(f"gate_days 样例: {gd[:3]}")

    mr = rep.get("monthly_returns") or []
    if mr:
        m0 = mr[0]
        mkey = "month" if "month" in m0 else ("date" if "date" in m0 else None)
        rkey = next((k for k in ("ret", "return", "pct") if k in m0), None)
        if mkey and rkey:
            neg = [m for m in mr if (m.get(rkey) or 0) < 0]
            worst_m = sorted(mr, key=lambda m: m.get(rkey) or 0)[:5]
            print(f"\n月度：负月 {len(neg)}/{len(mr)}；最差 5 个月: "
                  + ", ".join(f"{m[mkey]}:{m[rkey]:+.1%}" for m in worst_m))
        else:
            print(f"\nmonthly_returns 样例: {m0}")


if __name__ == "__main__":
    main()
