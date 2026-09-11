# -*- coding: utf-8 -*-
"""复权因子健康检查（数据治理 L4 门禁）。

坏因子静默入库的防线：① 覆盖缺失（日线有而因子无）；② 1.0 占位泛滥
（factor==1.0 且非上市首日）；③ 断崖（环比 |Δ|>30% 且当日真实涨跌 <2%
——真实除权必有价格调整，价格平稳的 factor 跳变必为数据错误）。
供 updater 更新后门禁、repair 工具前后对比、回测创建预检复用。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import polars as pl

from . import store

# 单股 factor==1.0 占比超过该阈值（2021 起）→ 进入 needs_refetch 名单
ONE_RATIO_ALERT = 0.80
# factor 环比断崖阈值
CLIFF_PCT = 0.30
# 断崖日真实涨跌小于该值 → 判定"价格平稳的假跳空"（数据错误而非真实除权）
REAL_MOVE_FLAT = 0.02


def check_adj_health(data_dir: str | None = None,
                     daily: pl.DataFrame | None = None,
                     adj: pl.DataFrame | None = None) -> dict:
    """复权因子健康检查。参数缺省时从 data_dir 读 parquet。

    返回:
        ok: 是否通过门禁（issues 为空）
        metrics: 各项量化指标
        issues: 文字化问题清单（空 = 健康）
        needs_refetch: 建议重拉因子的股票名单
    """
    if adj is None:
        adj = store.read_adj_factor(data_dir)
    if adj is None or adj.height == 0:
        return {"ok": False, "metrics": {}, "issues": ["adj_factor 表为空"],
                "needs_refetch": []}
    if daily is None:
        daily = store.read_daily(data_dir=data_dir)
    if daily is None or daily.height == 0:
        return {"ok": False, "metrics": {}, "issues": ["daily 表为空"],
                "needs_refetch": []}
    adj = adj.sort(["code", "date"])
    issues: list[str] = []

    # ---- 1. 覆盖缺失：日线有而因子无 ----
    daily_keys = daily.select(["code", "date"]).unique()
    missing = daily_keys.join(adj.select(["code", "date"]),
                              on=["code", "date"], how="anti")
    missing_n = missing.height

    # ---- 2. 1.0 占位占比（按年，2021 起）----
    a = adj.with_columns(pl.col("date").str.slice(0, 4).alias("yr"),
                         (pl.col("adj_factor") == 1.0).alias("is1"))
    yearly = {r["yr"]: r["n1"] / max(1, r["n"])
              for r in a.group_by("yr").agg(pl.len().alias("n"),
                                            pl.col("is1").sum().alias("n1")
                                            ).sort("yr").iter_rows(named=True)}
    bad_years = {y: round(v, 3) for y, v in yearly.items()
                 if y >= "2021" and v > ONE_RATIO_ALERT}

    # ---- 3. 断崖检测：环比 |Δ|>30%，且当日真实涨跌 <2%（假跳空）----
    a = a.with_columns(pl.col("adj_factor").pct_change().over("code").alias("chg"))
    a = a.join(daily.select(["code", "date", "close"]).with_columns(
        pl.col("close").pct_change().over("code").alias("real_chg")),
        on=["code", "date"], how="left")
    cliffs = a.filter(pl.col("chg").abs() > CLIFF_PCT)
    cliff_fake = cliffs.filter(
        pl.col("real_chg").abs() < REAL_MOVE_FLAT)
    cliff_days = Counter(cliffs["date"].to_list())

    # ---- 4. needs_refetch 名单：单股 1.0 占比超标（2021 起）----
    per_code = (a.filter(pl.col("yr") >= "2021")
                .group_by("code").agg(pl.len().alias("n"),
                                      pl.col("is1").sum().alias("n1"))
                .with_columns((pl.col("n1") / pl.col("n")).alias("ratio")))
    needs = per_code.filter(pl.col("ratio") > ONE_RATIO_ALERT) \
                    .sort("ratio", descending=True)

    metrics = {
        "adj_rows": adj.height,
        "missing_rows": missing_n,
        "one_ratio_by_year": {y: round(v, 3) for y, v in yearly.items()
                              if y >= "2020"},
        "bad_years": bad_years,
        "cliff_total": cliffs.height,
        "cliff_fake_flat_price": cliff_fake.height,
        "cliff_top_days": dict(cliff_days.most_common(5)),
        "needs_refetch_count": needs.height,
    }
    if missing_n > 0:
        issues.append(f"因子覆盖缺失 {missing_n} 行（日线有而因子无）")
    if bad_years:
        issues.append(f"1.0 占比超标年份: {bad_years}（阈值 {ONE_RATIO_ALERT:.0%}）")
    if cliff_fake.height > 0:
        top = dict(Counter(cliff_fake["date"].to_list()).most_common(3))
        issues.append(f"疑似假断崖（factor 跳变但价格平稳）{cliff_fake.height} 行，"
                      f"集中日: {top}")
    if needs.height > 0:
        issues.append(f"needs_refetch {needs.height} 只（1.0 占比 > {ONE_RATIO_ALERT:.0%}）")

    return {"ok": not issues, "metrics": metrics, "issues": issues,
            "needs_refetch": needs["code"].to_list() if needs.height else []}


def fmt_health_report(h: dict) -> str:
    """健康检查结果 -> 可读文本（更新日志 / 修复报告复用）。"""
    m = h["metrics"]
    lines = [f"健康检查: {'✅ 通过' if h['ok'] else '❌ 异常'}"]
    for it in h["issues"]:
        lines.append(f"  - {it}")
    lines.append(f"  因子行 {m.get('adj_rows', 0)} | 缺失 {m.get('missing_rows', 0)} "
                 f"| 断崖 {m.get('cliff_total', 0)}（假断崖 {m.get('cliff_fake_flat_price', 0)}）"
                 f"| needs_refetch {m.get('needs_refetch_count', 0)}")
    return "\n".join(lines)
