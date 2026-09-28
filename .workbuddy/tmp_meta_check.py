# -*- coding: utf-8 -*-
"""检查 stock_basic / index_daily / trade_calendar 的覆盖与时效"""
import sys, pathlib, datetime
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store

DATA = str(ROOT / "data")
daily_all = store.read_daily(None, DATA)
print(f"个股日K最新: {daily_all['date'].max()} | 码数 {daily_all['code'].n_unique()}", flush=True)

# 1) stock_basic
basic = store.read_stock_basic(DATA)
print(f"\n[stock_basic] {basic.height} 行, 列: {basic.columns}", flush=True)
# 2022 后上市的次新是否在列？
new_ipo = ["001280", "001391", "300784", "301173", "301217", "301275", "301392",
           "301449", "301458", "301479", "301682", "301687"]
in_basic = [c for c in new_ipo if basic.filter(pl.col("code") == c).height]
print(f"  12 只 2024-2026 次新在列: {len(in_basic)}/12", flush=True)
if "list_date" in basic.columns:
    null_ld = basic.filter(pl.col("list_date").is_null()).height
    print(f"  list_date 为空: {null_ld} 行", flush=True)
    recent = basic.filter(pl.col("list_date") >= "2026-09-01").height
    print(f"  2026-09 后上市(库内): {recent} 行", flush=True)
if "delisted" in basic.columns:
    print(f"  delisted=True: {basic.filter(pl.col('delisted')).height} 行", flush=True)

# 2) index_daily
try:
    idx = pl.read_parquet(ROOT / "data" / "index_daily.parquet")
    print(f"\n[index_daily] {idx.height} 行, 列: {idx.columns}", flush=True)
    if "index_key" in idx.columns:
        for k, sub in idx.group_by("index_key"):
            print(f"  {k}: {sub.height} 行, {sub['date'].min()} ~ {sub['date'].max()}", flush=True)
    else:
        print(f"  {idx['date'].min()} ~ {idx['date'].max()}", flush=True)
except Exception as e:
    print(f"\n[index_daily] 读取失败: {e}", flush=True)

# 3) trade_calendar
cal = store.read_calendar(DATA)
print(f"\n[trade_calendar] {cal.height} 行, {cal['date'].min()} ~ {cal['date'].max()}", flush=True)

# 4) 2022-01-04 后上市但库里没有的股票（baostock 对照）
from app.data.sources import BaostockSource
bs = BaostockSource()
def _q():
    rs = bs._bs.query_all_stock(day=END if (END := str(daily_all["date"].max())) else "2026-09-24")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q)
if res:
    fields, rows = res
    i_code, i_name = fields.index("code"), fields.index("code_name")
    live = {r[i_code].split(".")[-1]: r[i_name] for r in rows}
    lib = set(daily_all["code"].unique().to_list())
    in_basic_set = set(basic["code"].to_list())
    only_live = [c for c in live if c not in lib]
    print(f"\n[对照] 最新交易日全市场在市 {len(live)} 只 | 库内日K {len(lib)} | 库外在市 {len(only_live)}", flush=True)
    if only_live[:15]:
        print("  库外在市样例:", [(c, live[c]) for c in sorted(only_live)[:15]], flush=True)
