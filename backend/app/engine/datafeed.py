# -*- coding: utf-8 -*-
"""数据读取：Parquet → 后复权 DataFrame + LRU 缓存
后复权价 = 原始价 * adj_factor（因子累计，最早日=1；合成数据恒为1）。
同时保留 raw_close 供展示换算。

P1 并行加载：逐票读取/复权 join 为独立任务，线程池并行执行
（polars 读取/变换主要释放 GIL，SSD+多核下 200 只分钟线加载提速数倍）；
LRU 缓存加锁保证多线程安全。
"""
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import polars as pl

from ..data import store

_CACHE: "OrderedDict[tuple, pl.DataFrame]" = OrderedDict()
_CACHE_MAX = 64
_CACHE_LOCK = threading.Lock()
# 单票加载为独立 I/O+CPU 任务；8 并发已足够 saturate SSD，避免线程过多争抢
_LOAD_WORKERS = max(4, min(8, os.cpu_count() or 4))


def _cached(key: tuple, loader) -> pl.DataFrame:
    """线程安全 LRU：命中加锁快路径；未命中在锁外执行 loader（并发下可能
    重复加载同一票，结果一致且幂等，属可接受代价）"""
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    df = loader()
    with _CACHE_LOCK:
        _CACHE[key] = df
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return df


def _pmap(fn: Callable, items: list) -> list:
    """小任务并行映射；单任务直接串行（省去线程池开销）"""
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=_LOAD_WORKERS) as ex:
        return list(ex.map(fn, items))


def _attach_adj(df: pl.DataFrame, adj: Optional[pl.DataFrame]) -> pl.DataFrame:
    """merge 复权因子并生成后复权 OHLC。

    adj 必须为日级序列（每个交易日一行，由 updater 从事件级因子展开）。
    日线 date 为 "YYYY-MM-DD"；5分钟线 date 为 "YYYY-MM-DD HH:MM"，
    统一取前 10 位交易日关联，确保分钟线也能正确复权（此前分钟线恒为 1.0）。
    """
    if adj is not None and adj.height:
        df = df.with_columns(pl.col("date").str.slice(0, 10).alias("_d"))
        # adj 的 date 同样归一化到交易日（兼容日级/分钟级两种写入）；
        # 去重避免分钟级 adj 同日多行造成 join 笛卡尔膨胀
        adj_d = (adj.select([pl.col("code"), pl.col("date").str.slice(0, 10).alias("_d"),
                             pl.col("adj_factor").cast(pl.Float64)])
                 .unique(subset=["code", "_d"], keep="last"))
        df = df.join(adj_d, on=["code", "_d"], how="left").drop("_d")
    else:
        df = df.with_columns(pl.lit(1.0).alias("adj_factor"))
    df = df.with_columns(pl.col("adj_factor").fill_null(1.0))
    return df.with_columns([
        (pl.col("open") * pl.col("adj_factor")).alias("open"),
        (pl.col("high") * pl.col("adj_factor")).alias("high"),
        (pl.col("low") * pl.col("adj_factor")).alias("low"),
        (pl.col("close") * pl.col("adj_factor")).alias("close"),
        pl.col("close").alias("raw_close"),
    ]).with_columns(pl.col("raw_close").alias("raw_close"))


def load_daily(codes: list[str], start: Optional[str] = None, end: Optional[str] = None,
               data_dir: Optional[str] = None) -> dict[str, pl.DataFrame]:
    """按股票返回窗口内后复权日线数据"""
    out: dict[str, pl.DataFrame] = {}
    all_daily = store.read_daily(codes, data_dir)
    if all_daily is None:
        return out
    adj = store.read_adj_factor(codes, data_dir)

    def _one(code: str):
        def _load(c=code):
            df = all_daily.filter(pl.col("code") == c)
            if start:
                df = df.filter(pl.col("date") >= start)
            if end:
                df = df.filter(pl.col("date") <= end)
            if df.height == 0:
                return df.clear()
            df_a = adj.filter(pl.col("code") == c) if adj is not None else None
            return _attach_adj(df.sort("date"), df_a)

        return code, _cached(("daily", code, str(start), str(end), str(data_dir)), _load)

    for code, df in _pmap(_one, list(codes)):
        if df.height:
            out[code] = df
    return out


def load_minute5(codes: list[str], start: Optional[str] = None, end: Optional[str] = None,
                 data_dir: Optional[str] = None) -> dict[str, pl.DataFrame]:
    """按股票返回窗口内后复权 5 分钟数据"""
    out: dict[str, pl.DataFrame] = {}
    adj = store.read_adj_factor(codes, data_dir)

    def _one(code: str):
        def _load(c=code):
            df = store.read_minute5(c, start, end, data_dir)
            if df is None or df.height == 0:
                return None
            df_a = adj.filter(pl.col("code") == c) if adj is not None else None
            return _attach_adj(df.sort("date"), df_a)

        return code, _cached(("minute5", code, str(start), str(end), str(data_dir)), _load)

    for code, df in _pmap(_one, list(codes)):
        if df is not None and df.height:
            out[code] = df
    return out


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
