# -*- coding: utf-8 -*-
"""把 10 只新上市股票追加进 stock_basic（官方 updater 不新增缺失码）。"""
import sys, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import BaostockSource

DATA = str(ROOT / "data")
NEW10 = ["601091", "601123", "603448", "688801", "688837",
         "301686", "301688", "301689", "301697", "301699"]

bs = BaostockSource()
def _q_all():
    rs = bs._bs.query_all_stock(day="2026-09-24")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
res = bs._run_query(_q_all)
fields, rows = res
i_code, i_name = fields.index("code"), fields.index("code_name")
live = {r[i_code].split(".")[-1]: r[i_name] for r in rows}

def _q_basic():
    rs = bs._bs.query_stock_basic()
    rows2 = []
    while rs.error_code == "0" and rs.next():
        rows2.append(rs.get_row_data())
    return rs, (rs.fields, rows2)
res2 = bs._run_query(_q_basic)
fields2, rows2 = res2
idx = {f: i for i, f in enumerate(fields2)}
ipo_map = {r[idx["code"]].split(".")[-1]: r[idx["ipoDate"]] for r in rows2}

basic = store.read_stock_basic(DATA)
cols = basic.columns
new_rows = []
for c in NEW10:
    if basic.filter(pl.col("code") == c).height:
        continue
    row = {col: None for col in cols}
    row["code"] = c
    row["name"] = live.get(c, "")
    row["st"] = "ST" in live.get(c, "").upper()
    row["list_date"] = ipo_map.get(c)
    row["delisted"] = False
    new_rows.append(row)

if new_rows:
    new_df = pl.DataFrame(new_rows).select(cols)
    merged = pl.concat([basic, new_df]).sort("code")
    store.write_stock_basic(merged, DATA)
    print(f"追加 {new_df.height} 行, 合并后 {merged.height} 行", flush=True)
else:
    print("无待追加", flush=True)

basic2 = store.read_stock_basic(DATA)
print(f"复验: {basic2.height} 行", flush=True)
for c in NEW10:
    r = basic2.filter(pl.col("code") == c)
    if r.height:
        x = r.to_dicts()[0]
        print(f"  {c}: {x['name']} list_date={x['list_date']} delisted={x['delisted']}", flush=True)
    else:
        print(f"  {c}: 仍缺失!", flush=True)
