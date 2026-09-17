# -*- coding: utf-8 -*-
"""5分钟线一次性缺口回补（龙头低吸策略数据准备，2026-09-17）。

缺口A：已有票尾部增量 —— start = 全库分钟线最后交易日众数 + 1 天
       （用众数对齐：个别长期停牌票的最后日期更旧，不应拉偏起点）
缺口B：缺失票（非退市全市场 - minute5 已有）全窗口回补，start = 2023-01-01
       （覆盖龙头策略验证窗口 2023-03 起的全部历史，baostock 深历史优先）

两步均经 updater.update_task 落库进度（tasks 表，界面可见），串行执行防限流。
用法：python scripts/_backfill_minute5.py
"""
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from app import config, db
from app.data import store, updater

BACKFILL_START_B = "2023-01-01"


def minute5_codes() -> set:
    """minute5 目录已有票集合（文件名 = code）"""
    return {p.stem for p in config.MINUTE5_DIR.glob("*.parquet")}


def aligned_last_day() -> str:
    """全库分钟线最后交易日的众数（YYYY-MM-DD）"""
    files = sorted(config.MINUTE5_DIR.glob("*.parquet"))

    def _max(fp: Path):
        try:
            return pl.scan_parquet(fp).select(pl.col("date").max()).collect().item()
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        vals = [v for v in ex.map(_max, files) if v]
    if not vals:
        raise RuntimeError("minute5 目录为空，无法确定对齐日")
    return Counter(v[:10] for v in vals).most_common(1)[0][0]


def _run_step(label: str, codes: list, start: str) -> dict:
    tid = "data_" + uuid.uuid4().hex[:12]
    db.create_task(tid, label, "data_update",
                   payload={"scope": "minute5", "codes_n": len(codes),
                            "start_date": start, "auto_backfill": True})
    print(f"[{label}] task={tid} codes={len(codes)} start={start}", flush=True)
    stats = updater.update_task(tid, "minute5", codes=codes, start_date=start)
    print(f"[{label}] done: {stats}", flush=True)
    return stats


def main() -> None:
    basic = store.read_stock_basic()
    if basic is None:
        raise RuntimeError("stock_basic 不存在，无法确定更新范围")
    all_codes = (basic.filter(~pl.col("delisted"))["code"].to_list()
                 if "delisted" in basic.columns else basic["code"].to_list())
    have = minute5_codes()
    missing = [c for c in all_codes if c not in have]

    last = aligned_last_day()
    start_a = (datetime.strptime(last, "%Y-%m-%d")
               + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"全市场 {len(all_codes)} 只，已有分钟线 {len(have & set(all_codes))} 只，"
          f"缺失 {len(missing)} 只；分钟线对齐日 {last} -> 缺口A start={start_a}",
          flush=True)

    step_a = sorted(have & set(all_codes))
    if step_a:
        _run_step("分钟线回补A：已有票尾部增量", step_a, start_a)
    if missing:
        _run_step("分钟线回补B：缺失票全窗口", missing, BACKFILL_START_B)
    print("回补全部完成", flush=True)


if __name__ == "__main__":
    main()
