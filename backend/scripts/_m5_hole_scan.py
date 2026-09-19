# -*- coding: utf-8 -*-
"""分钟线全市场真缺口量化：日线有 bar 而 minute5 缺该日 => 真缺口；
日线也无 bar => 停牌（合法缺失）。输出缺口票清单与区段。
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl
from concurrent.futures import ThreadPoolExecutor

from app import config
from app.data import store

WIN_START = "2024-01-02"
END = "2026-09-18"


def minute_days(p: Path):
    try:
        df = pl.read_parquet(p, columns=["date"])
        days = sorted({x[:10] for x in df["date"].to_list() if WIN_START <= x[:10] <= END})
        return p.stem, days
    except Exception:
        return p.stem, []


def main():
    basic = store.read_stock_basic()
    inmarket = set(basic.filter(~pl.col("delisted"))["code"].to_list())
    daily = store.read_daily()
    daily_pairs = (daily.filter((pl.col("date") >= WIN_START)
                                & (pl.col("date") <= END))
                   .select(["code", "date"]).unique()
                   if daily is not None else None)
    files = sorted(config.MINUTE5_DIR.glob("*.parquet"))
    with ThreadPoolExecutor(8) as ex:
        mdays = dict(ex.map(minute_days, files))
    # 真缺口：日线有、分钟无
    hole_rows = []
    if daily_pairs is not None:
        md = pl.DataFrame({"code": [c for c in mdays for _ in mdays[c]],
                           "date": [d for c in mdays for d in mdays[c]]},
                          schema={"code": pl.String, "date": pl.String})
        holes = (daily_pairs.filter(pl.col("code").is_in(list(inmarket)))
                 .join(md, on=["code", "date"], how="anti")
                 .sort(["code", "date"]))
        hole_rows = holes.rows()
    by_code = {}
    for c, d in hole_rows:
        by_code.setdefault(c, []).append(d)
    # 压缩区段
    def blocks(ds):
        out = []
        for d in ds:
            if out and d <= out[-1][1]:
                out[-1][1] = d
            else:
                out.append([d, d])
        return [(a, b) for a, b in out]
    print(f"=== 全市场 minute5 真缺口（日线有bar而分钟缺日，{WIN_START}~{END}）===")
    print(f"缺口票 {len(by_code)} 只，缺口日合计 {len(hole_rows)} 天")
    for c in sorted(by_code, key=lambda x: -len(by_code[x]))[:25]:
        n = len(by_code[c])
        bs = blocks(by_code[c])
        bs_s = bs if len(bs) <= 4 else bs[:3] + [f"...共{len(bs)}段"]
        print(f"  {c}: 缺 {n} 天 {bs_s}")


if __name__ == "__main__":
    main()
