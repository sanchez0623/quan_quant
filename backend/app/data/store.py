# -*- coding: utf-8 -*-
"""Parquet 数据存储层
DATA_DIR 结构：
  daily.parquet            code,date(str YYYY-MM-DD),open,high,low,close,volume,amount（不复权原始价）
  minute5/{code}.parquet   code,date(str YYYY-MM-DD HH:mm),open,high,low,close,volume,amount
  adj_factor.parquet       code,date,adj_factor（后复权累计因子）
  trade_calendar.parquet   date,is_open(int)
  stock_basic.parquet      code,name,st(bool),list_date
"""
from pathlib import Path
from typing import Optional

import polars as pl

from .. import config


def data_root(data_dir: Optional[str] = None) -> Path:
    return Path(data_dir) if data_dir else config.DATA_DIR


# ---------------- daily ----------------

def write_daily(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    d = data_root(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "daily.parquet")


def read_daily(codes: Optional[list[str]] = None,
               data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "daily.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if codes:
        df = df.filter(pl.col("code").is_in(codes))
    return df if df.height else (df.clear() if codes else df)


# ---------------- minute5 ----------------

def write_minute5(code: str, df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    d = data_root(data_dir) / "minute5"
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / f"{code}.parquet")


def read_minute5(code: str, start: Optional[str] = None, end: Optional[str] = None,
                 data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "minute5" / f"{code}.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if start:
        df = df.filter(pl.col("date") >= start)
    if end:
        df = df.filter(pl.col("date") <= end + " 23:59" if len(end) == 10 else pl.col("date") <= end)
    return df


def list_minute5_codes(data_dir: Optional[str] = None) -> list[str]:
    d = data_root(data_dir) / "minute5"
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


# ---------------- adj_factor ----------------

def write_adj_factor(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    data_root(data_dir).mkdir(parents=True, exist_ok=True)
    df.write_parquet(data_root(data_dir) / "adj_factor.parquet")


def read_adj_factor(codes: Optional[list[str]] = None,
                    data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "adj_factor.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if codes:
        df = df.filter(pl.col("code").is_in(codes))
    return df


# ---------------- trade_calendar ----------------

def write_calendar(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    data_root(data_dir).mkdir(parents=True, exist_ok=True)
    df.write_parquet(data_root(data_dir) / "trade_calendar.parquet")


def read_calendar(data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "trade_calendar.parquet"
    return pl.read_parquet(p) if p.exists() else None


# ---------------- stock_basic ----------------

def write_stock_basic(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    data_root(data_dir).mkdir(parents=True, exist_ok=True)
    # 统一存纯数字 code（兼容历史 sh.600000 格式输入）
    df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    df.write_parquet(data_root(data_dir) / "stock_basic.parquet")


def read_stock_basic(data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "stock_basic.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    # 历史文件可能是 sh.600000 格式：读取时归一化为纯数字（与 daily/minute5 一致）
    if df.height and "." in str(df["code"][0]):
        df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    # 兼容旧 schema（无 delisted 列）：补齐默认 False
    if "delisted" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("delisted"))
    return df


# ---------------- index_constituents（指数成分长表） ----------------
# index_key(code),code,name,update_date,snapshot_date
# 全量替换语义：每次更新整体覆盖（避免残留已调出指数的股票）

def write_index_constituents(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    d = data_root(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    df.write_parquet(d / "index_constituents.parquet")


def read_index_constituents(data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "index_constituents.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if df.height and "." in str(df["code"][0]):
        df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    return df


# ---------------- stock_industry（申万 2021 三级行业） ----------------
# code,sw_l1,sw_l2,sw_l3,sw_code,snapshot_date；一票一行（无行业数据的票不写行）

def write_stock_industry(df: pl.DataFrame, data_dir: Optional[str] = None) -> None:
    d = data_root(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    df.write_parquet(d / "stock_industry.parquet")


def read_stock_industry(data_dir: Optional[str] = None) -> Optional[pl.DataFrame]:
    p = data_root(data_dir) / "stock_industry.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if df.height and "." in str(df["code"][0]):
        df = df.with_columns(pl.col("code").str.replace(r"^(sh|sz|bj)\.", "").alias("code"))
    return df


# ---------------- 统计（/api/data/status 用） ----------------

def parquet_stats_daily(data_dir: Optional[str] = None) -> Optional[dict]:
    df = read_daily(None, data_dir)
    if df is None or df.height == 0:
        return None
    return {"rows": int(df.height), "stocks": int(df["code"].n_unique()),
            "start": str(df["date"].min()), "end": str(df["date"].max()),
            "updated_at": _mtime(data_root(data_dir) / "daily.parquet")}


def parquet_stats_minute5(data_dir: Optional[str] = None) -> Optional[dict]:
    root = data_root(data_dir) / "minute5"
    codes = list_minute5_codes(data_dir)
    if not codes:
        return None
    # 行数用 parquet 元数据统计（不加载数据体），起止日期抽样首个文件
    import pyarrow.parquet as pq
    rows = 0
    for c in codes:
        fp = root / f"{c}.parquet"
        if fp.exists():
            try:
                rows += pq.read_metadata(str(fp)).num_rows
            except Exception:  # noqa: BLE001
                pass
    start = end = None
    try:
        d = (pl.scan_parquet(str(root / f"{codes[0]}.parquet"))
             .select(pl.col("date").min().alias("s"), pl.col("date").max().alias("e"))
             .collect())
        start, end = (d["s"][0] or "")[:10], (d["e"][0] or "")[:10]
    except Exception:  # noqa: BLE001
        pass
    return {"stocks": len(codes), "rows": rows, "start": start, "end": end,
            "updated_at": _mtime(root / f"{codes[0]}.parquet")}


def parquet_stats_adj_factor(data_dir: Optional[str] = None) -> Optional[dict]:
    p = data_root(data_dir) / "adj_factor.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    return {"rows": int(df.height), "updated_at": _mtime(p)}


def parquet_stats_calendar(data_dir: Optional[str] = None) -> Optional[dict]:
    df = read_calendar(data_dir)
    if df is None or df.height == 0:
        return None
    open_days = df.filter(pl.col("is_open") == 1)
    if open_days.height == 0:
        return None
    return {"start": str(open_days["date"].min()), "end": str(open_days["date"].max())}


def parquet_stats_index(data_dir: Optional[str] = None) -> Optional[dict]:
    p = data_root(data_dir) / "index_constituents.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if df.height == 0:
        return None
    return {"rows": int(df.height), "stocks": int(df["code"].n_unique()),
            "snapshot_date": str(df["snapshot_date"].max()),
            "updated_at": _mtime(p)}


def parquet_stats_industry(data_dir: Optional[str] = None) -> Optional[dict]:
    p = data_root(data_dir) / "stock_industry.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p)
    if df.height == 0:
        return None
    return {"rows": int(df.height), "stocks": int(df["code"].n_unique()),
            "l3_count": int(df["sw_code"].n_unique()),
            "snapshot_date": str(df["snapshot_date"].max()),
            "updated_at": _mtime(p)}


def parquet_stats_stock_basic(data_dir: Optional[str] = None) -> Optional[dict]:
    """股票列表统计：总数 / ST 数 / 退市数（供数据管理页展示）"""
    df = read_stock_basic(data_dir)
    if df is None or df.height == 0:
        return None
    return {"total": int(df.height),
            "st_count": int(df.filter(pl.col("st")).height) if "st" in df.columns else 0,
            "delisted_count": int(df.filter(pl.col("delisted")).height),
            "updated_at": _mtime(data_root(data_dir) / "stock_basic.parquet")}


def _mtime(p: Path) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
