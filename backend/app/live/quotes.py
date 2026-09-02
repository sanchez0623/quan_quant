# -*- coding: utf-8 -*-
"""盘中实时行情多源接入（LIVE_SIGNAL_SYSTEM §4，2026-09 实测定稿）。

- fetch_minute5(code, day)：当日 5 分钟 bar。主源 mootdx（TCP 7709，免疫
  DNS/代理/WAF）→ 备源新浪（CN_MarketData scale=5）；两源 volume 均为"股"。
- completed_bars(df, now)："完成 bar"判定。TDX/新浪约定 bar 戳=结束时刻，
  戳 <= now 即完成；戳 > now 为进行中 bar（仅展示，不喂状态机——§4.4
  信号只由完成 bar 触发，避免同 bar 内信号抖动）。
- realtime_quotes(codes)：qt.gtimg.cn 实时报价（GBK，~50ms），用于
  交叉校验（偏离>1% 暂停该票信号）与除权检测（昨收 vs 日线库收盘）。

代理环境教训（§4.3）：所有 http 请求一律 trust_env=False（禁系统代理直连）。
"""
import json
from datetime import datetime, timedelta
from typing import Optional

import polars as pl

from ..data import sources

# 完成bar缓冲：bar 戳后预留秒数（收盘集合竞价 15:00 bar 戳后立即视为完成）
BAR_LAG_SEC = 0


def fetch_minute5(code: str, day: str) -> Optional[pl.DataFrame]:
    """code 在 day（含回看 7 日兜底）的 5 分钟 bar，返回列
    code/date(YYYY-MM-DD HH:MM)/open/high/low/close/volume/amount（股口径）。
    主源失败自动切备源；全部失败返回 None。"""
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    for src in sources.SOURCES:
        if src.name not in ("mootdx", "sina"):
            continue  # 盘中链路只走已实测的两源（baostock 盘后才出当日数据）
        try:
            df = src.get_minute5(code, start, day)
        except Exception:
            df = None
        if df is not None and df.height:
            out = df.filter(pl.col("date").str.slice(0, 10) == day).sort("date")
            if out.height:
                return out
    return None


def completed_bars(df: pl.DataFrame, now: Optional[datetime] = None) -> pl.DataFrame:
    """只保留完成 bar（bar 戳=结束时刻：戳+缓冲 <= now；戳未来者为进行中 bar）"""
    now = now or datetime.now()
    cut = (now + timedelta(seconds=BAR_LAG_SEC)).strftime("%Y-%m-%d %H:%M")
    return df.filter(pl.col("date") <= cut)


def _qt_symbol(code: str) -> Optional[str]:
    """纯数字代码 -> qt.gtimg 带市场前缀（6=sh，0/3=sz，4/8/9=bj）"""
    c = str(code).strip()
    if c.startswith("6"):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    if c.startswith(("4", "8", "9")):
        return f"bj{c}"
    return None


def realtime_quotes(codes: list[str], timeout: float = 5.0) -> dict[str, dict]:
    """qt.gtimg.cn 实时报价：{code: {name, price, prev_close}}；失败返回 {}。

    返回字段序（实测）：1=名称 3=现价 4=昨收。GBK 编码。"""
    if not codes:
        return {}
    syms = [s for s in (_qt_symbol(c) for c in codes) if s]
    if not syms:
        return {}
    try:
        sess = sources._no_session_proxies()
        if sess is None:
            return {}
        r = sess.get("http://qt.gtimg.cn/q=" + ",".join(syms), timeout=timeout)
        if r.status_code != 200:
            return {}
        text = r.content.decode("gbk", errors="replace")
        out: dict[str, dict] = {}
        for line in text.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            key, _, val = line.partition('="')
            code = key.replace("v_", "")
            for pre in ("sh", "sz", "bj"):
                if code.startswith(pre):
                    code = code[len(pre):]
                    break
            parts = val.rstrip('"').split("~")
            if len(parts) < 5:
                continue
            try:
                out[code] = {"name": parts[1], "price": float(parts[3]),
                             "prev_close": float(parts[4])}
            except ValueError:
                continue
        return out
    except Exception:
        return {}


def check_bar_divergence(bar_close: float, qt: dict, tol: float = 0.01) -> Optional[str]:
    """交叉校验：最新完成 bar 收盘 vs qt 实时价偏离超容差 -> 告警文案（None=通过）"""
    if not qt or not qt.get("price"):
        return None
    dev = abs(bar_close - qt["price"]) / max(qt["price"], 1e-9)
    if dev > tol:
        return (f"数据校验失败：bar收盘 {bar_close} vs 实时 {qt['price']} "
                f"偏离 {dev * 100:.2f}%（>{tol * 100:.0f}%）")
    return None


def check_adj_mismatch(qt: dict, db_close: Optional[float],
                       tol: float = 0.002) -> Optional[str]:
    """除权检测：qt 昨收 vs 日线库 as_of 收盘不一致 -> 疑似除权/数据错位。
    返回告警文案（None=通过）。除权日只发提示、不发交易信号（§4.3）。"""
    if not qt or not qt.get("prev_close") or not db_close:
        return None
    dev = abs(qt["prev_close"] - db_close) / max(db_close, 1e-9)
    if dev > tol:
        return (f"昨收不一致：实时昨收 {qt['prev_close']} vs 日线库 {db_close} "
                f"偏离 {dev * 100:.2f}%——疑似除权日，今日只提示不产交易信号")
    return None
