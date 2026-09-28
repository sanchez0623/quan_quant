# -*- coding: utf-8 -*-
"""停牌日成交影响对照实验（只读本地数据，不拉取K线）。

基线 = 现状引擎（本地日线含停牌冻结行 volume=null，引擎按正常 bar 处理，
       信号/止损可在冻结价成交）。
对照 = 影子数据目录：daily.parquet 剔除停牌行，其余 parquet 硬链接同文件。
       引擎既有 `i is None: continue` 机制自然生效 -> 挂单/止损顺延至
       复牌日开盘成交（贴近实盘语义）。

基底任务：bt_a5945a94c500 龙头低吸基线-默认参数-全区间（2024-01-02 -> 2026-09-18，
全市场静态池 5211 只）。两臂同一 config，唯一差异 = 停牌行是否存在。
结论落库三件套 + tag=重点。
"""
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db                                # noqa: E402
from app.data import store                        # noqa: E402
from app.engine import datafeed, runner           # noqa: E402

BASE_TASK = "bt_a5945a94c500"
ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "data" / "reports"
OUT = Path(__file__).resolve().parent / "out"
SHADOW = OUT / "shadow_nosusp"
METRIC_KEYS = ["total_return", "annual_return", "max_drawdown", "sharpe",
               "win_rate", "profit_loss_ratio", "total_trades", "total_pnl",
               "stop_loss_pnl", "commission_total"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_shadow() -> set:
    src = store.data_root()
    SHADOW.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.parquet"):
        if f.name == "daily.parquet":
            continue
        link = SHADOW / f.name
        if link.exists():
            continue
        try:
            os.link(f, link)
        except OSError:
            shutil.copy2(f, link)
    daily = pl.read_parquet(src / "daily.parquet")
    susp = daily.filter(pl.col("volume").is_null())
    dropped = set(zip(susp["code"].to_list(), susp["date"].to_list()))
    clean = daily.filter(pl.col("volume").is_not_null())
    clean.write_parquet(SHADOW / "daily.parquet")
    log(f"影子目录就绪: {SHADOW}  剔除停牌行 {clean.height}/{daily.height} 差 {len(dropped)}")
    return dropped


def run_one(cfg: dict, data_dir=None, tag: str = "") -> dict:
    datafeed.clear_cache()
    last = {"v": -10}

    def cb(v, msg=""):
        if v - last["v"] >= 10:
            last["v"] = v
            log(f"{tag} progress {v:.0f}% {msg}")

    t0 = time.time()
    rep = runner.run_backtest(json.loads(json.dumps(cfg)), data_dir=data_dir,
                              progress_cb=cb)
    log(f"{tag} 完成，用时 {time.time() - t0:.0f}s")
    return rep


def resume_open_map(codes: set, daily: pl.DataFrame, dropped: set) -> dict:
    """(code, suspended_date) -> 复牌后首个真实 bar 的原始开盘价。
    daily.parquet 的 open 即原始价（复权是 datafeed 运行时乘 factor），可直接比较。"""
    out = {}
    if not codes:
        return out
    sub = daily.filter(pl.col("code").is_in(list(codes)))
    for code in codes:
        df = sub.filter(pl.col("code") == code).sort("date")
        if df.height == 0:
            continue
        real_dates = set(df.filter(pl.col("volume").is_not_null())["date"].to_list())
        opens = list(zip(df["date"].to_list(), df["open"].to_list()))
        for d, o in opens:
            if (code, d) in dropped:
                nxt = next(((dd, oo) for dd, oo in opens if dd > d and dd in real_dates),
                           None)
                if nxt:
                    out[(code, d)] = nxt[1]
    return out


def susp_fill_analysis(rep: dict, dropped: set, resume_map: dict) -> dict:
    rows, bias = [], 0.0
    for t in rep.get("trade_log") or []:
        key = (t["code"], t["time"][:10])
        if key not in dropped:
            continue
        cf = resume_map.get(key)
        adv = None
        if cf:
            adv = ((t["price"] - cf) if t["side"] == "sell" else (cf - t["price"])) * t["volume"]
            bias += adv
        rows.append({"code": t["code"], "name": t.get("name"), "date": t["time"][:10],
                     "side": t["side"], "type": t["type"], "price": t["price"],
                     "volume": t["volume"], "resume_open": cf, "advantage": adv})
    return {"count": len(rows), "bias_rmb": round(bias, 2), "rows": rows}


def register(name: str, cfg: dict, rep: dict) -> str:
    tid = "bt_" + uuid.uuid4().hex[:12]
    path = REPORTS / f"{tid}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    payload = {"strategy_id": cfg.get("strategy_id", ""), "period": cfg.get("period", ""),
               "config": cfg, "report_path": str(path)}
    db.create_task(tid, name, "backtest", payload, tag="重点")
    db.save_report(tid, str(path))
    db.update_task(tid, status="success", progress=100, message="")
    log(f"落库 {tid} {name}")
    return tid


def main() -> None:
    OUT.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    task = db.get_task(BASE_TASK)
    cfg = (task.get("payload") or {})["config"]
    stored_metrics = json.load(open(task["payload"]["report_path"], encoding="utf-8"))["metrics"]

    dropped = build_shadow()

    log("=== 臂1/基线：现状引擎（默认数据目录） ===")
    rep0 = run_one(cfg, None, "基线")
    log("=== 臂2/对照：影子目录（剔除停牌行） ===")
    rep1 = run_one(cfg, str(SHADOW), "对照")

    m0, m1 = rep0["metrics"], rep1["metrics"]
    drift = (m0["total_return"] - stored_metrics["total_return"])
    log(f"漂移提示: 基线重跑 total_return {m0['total_return']:+.4%} vs 落库任务 "
        f"{stored_metrics['total_return']:+.4%}（落库后引擎/数据有演化，"
        f"差 {drift:+.4%}；A/B 有效性基于同代码重跑，不受影响）")

    daily = pl.read_parquet(store.data_root() / "daily.parquet",
                            columns=["code", "date", "open", "volume"])
    window = (cfg["start_date"], cfg["end_date"])
    uni = set(cfg.get("universe") or [])
    dropped_win = sum(1 for c, d in dropped if window[0] <= d <= window[1])
    codes_w = {c for (c, d) in dropped if window[0] <= d <= window[1] and c in uni}
    resume_map = resume_open_map(codes_w, daily, dropped)

    a0 = susp_fill_analysis(rep0, dropped, resume_map)
    a1 = susp_fill_analysis(rep1, dropped, resume_map)

    lines = ["# 停牌日成交影响对照实验",
             f"- 生成: {datetime.now().isoformat(timespec='seconds')}",
             f"- 基底: {BASE_TASK} 龙头低吸基线-默认参数-全区间 "
             f"({window[0]} -> {window[1]})，静态池 {len(uni)} 只",
             f"- 停牌冻结行（volume=null，全库 {len(dropped)} 行，窗口内涉及 {dropped_win} 行）",
             f"- 对照语义: 影子数据目录剔除停牌行 -> 挂单/止损顺延复牌日开盘成交",
             f"- 基线臂以当前代码重跑：total_return {m0['total_return']:+.2%}，"
             f"与落库任务 {stored_metrics['total_return']:+.2%} 存在漂移"
             f"（任务落库后引擎/数据演化）；A/B 两臂同代码同数据集，内部对比有效",
             "",
             "## 指标对比",
             "| 指标 | 基线(现状) | 对照(剔除停牌) | 差异 |",
             "|---|---|---|---|"]
    for k in METRIC_KEYS:
        v0, v1 = m0.get(k), m1.get(k)
        d = (v1 - v0) if isinstance(v0, (int, float)) and isinstance(v1, (int, float)) else None
        ds = f"{d:+.4g}" if d is not None else "-"
        lines.append(f"| {k} | {v0} | {v1} | {ds} |")
    lines += ["",
              f"## 基线臂落在停牌 bar 的成交（冻结价成交明细）: {a0['count']} 笔，"
              f"基线相对复牌价的优势合计 {a0['bias_rmb']:+,.0f} 元（正=基线占便宜/虚高）",
              f"## 对照臂落在停牌 bar 的成交（应为 0）: {a1['count']} 笔",
              "", "## 冻结价成交明细（基线臂）",
              "| code | 名称 | 日期 | 方向 | 类型 | 成交价 | 股数 | 复牌开盘 | 优势(元) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in a0["rows"]:
        lines.append(f"| {r['code']} | {r['name']} | {r['date']} | {r['side']} | "
                     f"{r['type']} | {r['price']} | {r['volume']} | "
                     f"{r['resume_open'] if r['resume_open'] is not None else '窗口末无复牌'} | "
                     f"{r['advantage'] if r['advantage'] is not None else '-'} |")
    md = OUT / f"suspend_impact_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    log(f"报告: {md}")

    t0 = dict(cfg)
    register("停牌成交对照-基线-现状引擎(冻结价成交)", t0, rep0)
    t1 = dict(cfg)
    register("停牌成交对照-剔除停牌行-顺延复牌成交", t1, rep1)

    print("\n===== 汇总 =====")
    for k in METRIC_KEYS:
        print(f"{k:20s} 基线 {m0.get(k)}  对照 {m1.get(k)}")
    print(f"冻结价成交笔数(基线): {a0['count']}  优势合计: {a0['bias_rmb']:+,.0f} 元")
    print(f"冻结价成交笔数(对照,应为0): {a1['count']}")


if __name__ == "__main__":
    main()
