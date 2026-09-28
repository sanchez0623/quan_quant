# -*- coding: utf-8 -*-
"""步骤1+2+3 一体脚本：
1) updater scope=stock_basic 刷新股票列表（在市/ST/退市标记）
2) 10 只新股东拉 日K+因子+5分钟K（从上市日=1990 起，baostock 自动从上市日返回）
3) list_date 回填（query_stock_basic ipoDate）
全程串行单连接。"""
import sys, time, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import updater, store
from app.data.sources import BaostockSource
from app.data.updater import _expand_adj_to_daily

DATA = str(ROOT / "data")
NEW10 = ["601091", "601123", "603448", "688801", "688837",
         "301686", "301688", "301689", "301697", "301699"]

def cb(p, m):
    print(f"[{p:5.1f}%] {m}", flush=True)

# ---- 1) stock_basic 刷新 ----
print("=== 步骤1: stock_basic 刷新 ===", flush=True)
# 今天(2026-09-28)非交易日/盘前，query_all_stock 为空：把"今天"指到最近交易日 2026-09-24
import app.data.updater as _upd
_orig_strftime = _upd.time.strftime
_upd.time.strftime = lambda *a, **k: "2026-09-24" if (not a or a[0] == "%Y-%m-%d") else _orig_strftime(*a, **k)
try:
    stats1 = updater.update(scope="stock_basic", progress_cb=cb)
finally:
    _upd.time.strftime = _orig_strftime
print(f"stats: {stats1}", flush=True)
basic = store.read_stock_basic(DATA)
print(f"刷新后: {basic.height} 行, 新10在列: "
      f"{sum(1 for c in NEW10 if basic.filter(pl.col('code') == c).height)}/10", flush=True)

# ---- 2) 新股东拉 K 线 ----
print("\n=== 步骤2: 新股东拉日线+因子+分钟 ===", flush=True)
stats2 = updater.update(scope="all", codes=NEW10,
                        start_date="1990-01-01",
                        progress_cb=cb)
print(f"stats: {stats2}", flush=True)

# ---- 3) list_date 回填 ----
print("\n=== 步骤3: list_date 回填 ===", flush=True)
bs = BaostockSource()
def _q():
    rs = bs._bs.query_stock_basic()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q)
ipo_map = {}
if res:
    fields, rows = res
    i_code, i_ipo = fields.index("code"), fields.index("ipoDate")
    for r in rows:
        ipo_map[r[i_code].split(".")[-1]] = r[i_ipo]
print(f"ipo_map: {len(ipo_map)} 只", flush=True)

basic = store.read_stock_basic(DATA)
filled = basic.with_columns(
    pl.col("code").replace_strict(ipo_map, default=None).alias("list_date"))
n_filled = filled.filter(pl.col("list_date").is_not_null()).height
store.write_stock_basic(filled, DATA)
print(f"list_date 回填: {n_filled}/{filled.height} 行非空", flush=True)

# ---- 复验 ----
print("\n=== 复验 ===", flush=True)
basic2 = store.read_stock_basic(DATA)
print(f"stock_basic: {basic2.height} 行, list_date 非空 {basic2.filter(pl.col('list_date').is_not_null()).height}", flush=True)
for c in NEW10:
    d = store.read_daily([c], DATA)
    m = store.read_minute5(c, data_dir=DATA)
    a = store.read_adj_factor([c], DATA)
    print(f"  {c}: 日K {'-'} 行, 分钟 {'有' if m is not None and m.height else '无'}, "
          f"因子 {'有' if a is not None and a.height else '无'}", flush=True)
print("=== ALL DONE ===", flush=True)
