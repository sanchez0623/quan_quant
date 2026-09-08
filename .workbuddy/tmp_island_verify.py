# -*- coding: utf-8 -*-
"""验证：孤点缺口日期在 daily 的 vol/amount 是否真实"""
import polars as pl
ROOT = r"d:\Sanchez\AI\TraeProjects\quan_quant"
daily = pl.read_parquet(ROOT + r"\data\daily.parquet")

cases = [
    ("000528", "2021-01-15"),
    ("000301", "2021-04-26"),
    ("000401", "2021-03-18"),
    ("600460", "2021-06-30"),
    ("000564", "2021-02-18"),
    ("600329", "2021-04-30"),
    ("000983", "2021-08-09"),
    ("600399", "2021-04-15"),
    ("002075", "2021-07-07"),
]
for c, d in cases:
    r = daily.filter((pl.col("code") == c) & (pl.col("date") == d))
    if r.height:
        x = r.to_dicts()[0]
        print(f"{c} {d}: close={x['close']} vol={x['volume']} amount={x['amount']} "
              f"prev_close={None}", flush=True)
    else:
        print(f"{c} {d}: 无日线", flush=True)
