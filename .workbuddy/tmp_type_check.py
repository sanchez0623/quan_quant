# -*- coding: utf-8 -*-
"""105 只库外码的 type 判定（1=股票 2=指数 3=其它）"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import BaostockSource

daily_all = store.read_daily(None, str(ROOT / "data"))
basic = store.read_stock_basic(str(ROOT / "data"))
basic_set = set(basic["code"].to_list())
END = str(daily_all["date"].max())

bs = BaostockSource()
def _q_all():
    rs = bs._bs.query_all_stock(day=END)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q_all)
fields, rows = res
i_code, i_name = fields.index("code"), fields.index("code_name")
A_PREFIX = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
missing = {}
for r in rows:
    code = r[i_code].split(".")[-1]
    if code.startswith(A_PREFIX) and code not in basic_set:
        missing[code] = r[i_name]

def _q_basic():
    rs = bs._bs.query_stock_basic()
    rows2 = []
    while rs.error_code == "0" and rs.next():
        rows2.append(rs.get_row_data())
    return rs, (rs.fields, rows2)
res2 = bs._run_query(_q_basic)
fields2, rows2 = res2
idx = {f: i for i, f in enumerate(fields2)}
type_map, status_map = {}, {}
for r in rows2:
    code = r[idx["code"]].split(".")[-1]
    type_map[code] = r[idx["type"]]
    status_map[code] = r[idx["status"]]

from collections import Counter
tc = Counter()
real_stocks = []
for c, name in missing.items():
    t = type_map.get(c, "?")
    tc[t] += 1
    if t == "1":
        real_stocks.append((c, name, status_map.get(c)))
print(f"库外 {len(missing)} 码 type 分布: {dict(tc)}", flush=True)
print(f"其中 type=1(真股票): {len(real_stocks)}", flush=True)
for c, n, s in real_stocks:
    print(f"    {c} {n} status={s}", flush=True)
