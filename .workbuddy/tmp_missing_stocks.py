# -*- coding: utf-8 -*-
"""确认：库外在市 2097 只中真 A 股个股数量（按代码前缀过滤）+ 新上市股票检查"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import BaostockSource

daily_all = store.read_daily(None, str(ROOT / "data"))
END = str(daily_all["date"].max())
basic = store.read_stock_basic(str(ROOT / "data"))

bs = BaostockSource()
def _q():
    rs = bs._bs.query_all_stock(day=END)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q)
fields, rows = res
i_code, i_name = fields.index("code"), fields.index("code_name")
A_PREFIX = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
lib = set(daily_all["code"].unique().to_list())
basic_set = set(basic["code"].to_list())
missing_stocks = []
for r in rows:
    code = r[i_code].split(".")[-1]
    name = r[i_name]
    if code.startswith(A_PREFIX) and code not in basic_set:
        missing_stocks.append((code, name))
print(f"在市但 stock_basic 缺失的真 A 股个股: {len(missing_stocks)}", flush=True)
for c, n in missing_stocks[:20]:
    print(f"    {c} {n}", flush=True)

# 2026-09-25 之后上市的新股（baostock 有、库无）——用 query_stock_basic 找
def _q2():
    rs = bs._bs.query_stock_basic()
    rows2 = []
    while rs.error_code == "0" and rs.next():
        rows2.append(rs.get_row_data())
    return rs, (rs.fields, rows2)
res2 = bs._run_query(_q2)
fields2, rows2 = res2
i_code2, i_ipo2, i_out = fields2.index("code"), fields2.index("ipoDate"), fields2.index("outDate")
new_after = []
for r in rows2:
    code = r[i_code2].split(".")[-1]
    ipo, out = r[i_ipo2], r[i_out]
    if (code.startswith(A_PREFIX) and ipo > END
            and (not out or out == "0000-00-00" or out > END)):
        new_after.append((code, ipo))
print(f"\n2026-09-24 之后上市的新股（库全无）: {len(new_after)}", flush=True)
for c, ipo in new_after[:20]:
    print(f"    {c} ipo={ipo}", flush=True)
