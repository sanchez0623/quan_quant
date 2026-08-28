# -*- coding: utf-8 -*-
"""合成演示数据生成：几何随机游走 + 缓慢趋势 + 偶发跳空；minute5 由日线插值+噪声。
幂等：重复调用覆盖写入。
"""
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import polars as pl

from . import store

# 默认股票池（未知代码命名为“演示股XXXX”）
DEFAULT_STOCKS = [
    ("600000", "浦发银行"),
    ("000001", "平安银行"),
    ("600036", "招商银行"),
    ("000858", "五粮液"),
    ("601318", "中国平安"),
]

# 5分钟bar时间点：9:35~11:30 与 13:05~15:00 各 24 根
_MORNING = [f"{9 + (35 + 5 * i) // 60:02d}:{(35 + 5 * i) % 60:02d}" for i in range(24)]
_AFTERNOON = [f"{13 + (5 + 5 * i) // 60:02d}:{(5 + 5 * i) % 60:02d}" for i in range(24)]
BAR_TIMES = _MORNING + _AFTERNOON  # 48 根


def trade_dates(days: int, end_date: Optional[date] = None) -> list[str]:
    """从 end_date（默认今天）往回推 days 个工作日（周六日休市，简化节假日处理）"""
    d = end_date or date.today()
    out = []
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return sorted(out)


def _gen_daily(code: str, days: int, rng: np.random.Generator) -> pl.DataFrame:
    """几何随机游走 + 缓慢趋势 + 偶发跳空；volume 对数正态"""
    dates = trade_dates(days)
    start_price = float(rng.uniform(8, 60))
    drift = float(rng.normal(0.0002, 0.0004))          # 缓慢趋势
    rets = rng.normal(drift, 0.018, size=days)          # 日收益率
    closes = start_price * np.exp(np.cumsum(rets))
    rows = {"code": [], "date": [], "open": [], "high": [], "low": [],
            "close": [], "volume": [], "amount": []}
    prev_close = closes[0] / (1 + rets[0])
    for i, dt in enumerate(dates):
        c = float(closes[i])
        gap = float(rng.normal(0, 0.01)) if rng.random() < 0.04 else 0.0  # 偶发跳空
        o = float(max(0.5, prev_close * (1 + gap)))
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.006)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        vol = float(np.exp(rng.normal(np.log(2e6), 0.5)))
        amt = vol * (o + hi + lo + c) / 4
        rows["code"].append(code); rows["date"].append(dt)
        rows["open"].append(round(o, 2)); rows["high"].append(round(hi, 2))
        rows["low"].append(round(lo, 2)); rows["close"].append(round(c, 2))
        rows["volume"].append(int(vol)); rows["amount"].append(round(amt, 2))
        prev_close = c
    return pl.DataFrame(rows)


def _gen_minute5(code: str, daily: pl.DataFrame, rng: np.random.Generator) -> pl.DataFrame:
    """由日线收盘价插值 + 噪声生成 48 根/日 5分钟线"""
    rows = {"code": [], "date": [], "open": [], "high": [], "low": [],
            "close": [], "volume": [], "amount": []}
    d = daily.to_dicts()
    for i, bar_d in enumerate(d):
        o_day, c_day = bar_d["open"], bar_d["close"]
        hi_day, lo_day = bar_d["high"], bar_d["low"]
        vol_day = bar_d["volume"]
        # 路径：开盘→收盘线性插值叠加噪声，约束在 [low, high] 附近
        path = np.linspace(o_day, c_day, 49)
        noise = rng.normal(0, 0.0022, size=49) * np.linspace(1.4, 1.0, 49)
        path = np.clip(path * (1 + noise), lo_day * 0.995, hi_day * 1.005)
        # 日内 U 型成交量
        u = np.linspace(1.6, 0.7, 24).tolist() + np.linspace(0.7, 1.8, 24).tolist()
        for k, hhmm in enumerate(BAR_TIMES):
            o_k = float(path[k]); c_k = float(path[k + 1])
            hi_k = max(o_k, c_k) + abs(float(rng.normal(0, 0.0008))) * o_k
            lo_k = min(o_k, c_k) - abs(float(rng.normal(0, 0.0008))) * o_k
            vol_k = vol_day * u[k] / 48.0
            rows["code"].append(code)
            rows["date"].append(f"{bar_d['date']} {hhmm}")
            rows["open"].append(round(o_k, 2)); rows["high"].append(round(hi_k, 2))
            rows["low"].append(round(lo_k, 2)); rows["close"].append(round(c_k, 2))
            rows["volume"].append(int(vol_k))
            rows["amount"].append(round(vol_k * c_k, 2))
        _ = i
    return pl.DataFrame(rows)


def generate_demo_data(stocks: Optional[list[str]] = None, days: int = 500,
                       data_dir: Optional[str] = None, seed: Optional[int] = None,
                       with_minute5: bool = True) -> dict:
    """生成合成演示数据（幂等覆盖）。stocks 传代码列表；返回统计信息。"""
    if stocks:
        from .sources import _norm_code
        known = dict(DEFAULT_STOCKS)
        stocks = [_norm_code(str(c).strip()) for c in stocks if str(c).strip()]
        pairs = [(c, known.get(c, f"演示股{c}")) for c in stocks]
    else:
        pairs = list(DEFAULT_STOCKS)
    rng = np.random.default_rng(seed)

    daily_frames, minute_frames = [], []
    for code, _name in pairs:
        d = _gen_daily(code, days, rng)
        daily_frames.append(d)
        if with_minute5:
            minute_frames.append((code, _gen_minute5(code, d, rng)))

    daily = pl.concat(daily_frames)
    dates = sorted(daily["date"].unique().to_list())
    calendar = pl.DataFrame({"date": dates, "is_open": [1] * len(dates)})
    # 复权因子全 1（合成数据无除权）
    adj_rows = []
    for code, _ in pairs:
        adj_rows.append(pl.DataFrame({"code": [code] * len(dates), "date": dates,
                                      "adj_factor": [1.0] * len(dates)}))
    adj = pl.concat(adj_rows)
    basic = pl.DataFrame({
        "code": [c for c, _ in pairs],
        "name": [n for _, n in pairs],
        "st": [False] * len(pairs),
        "list_date": ["2010-01-01"] * len(pairs),
    })

    store.write_daily(daily, data_dir)
    store.write_calendar(calendar, data_dir)
    store.write_adj_factor(adj, data_dir)
    store.write_stock_basic(basic, data_dir)
    n_minute_rows = 0
    if minute_frames:
        for code, m in minute_frames:
            store.write_minute5(code, m, data_dir)
            n_minute_rows += m.height

    return {"stocks": len(pairs), "days": len(dates), "daily_rows": daily.height,
            "minute5_rows": n_minute_rows,
            "start": dates[0], "end": dates[-1],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
