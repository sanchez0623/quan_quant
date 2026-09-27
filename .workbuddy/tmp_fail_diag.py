# -*- coding: utf-8 -*-
"""诊断失败码：上市日 vs 2022-01-04 关系"""
import sys, pathlib
from collections import Counter
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data.sources import BaostockSource

# 失败码 = 本轮 todo 且 done 中仍有缺口的（直接看 done 里哪些码对应区间仍空）
# 简化：读 daily，找 2022-01-04 至今无任何数据的码（= 上市晚于 2022 或退市）
daily_all = store.read_daily(None, str(ROOT / "data"))
first_map = {r["code"]: r["first"] for r in
             daily_all.group_by("code").agg(pl.col("date").min().alias("first")).to_dicts()}

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
print(f"ipo_map: {len(ipo_map)}", flush=True)

# 本轮的失败码（从 stdout 抄录）— 用二次诊断：这些码 2022-01-04 前是否有数据
fail_codes = ["001280", "001391", "300784", "301173", "301217", "301275", "301392",
              "301449", "301458", "301479", "301551", "301590", "301601", "301539",
              "301636", "301633", "301609", "301622", "301608", "301587", "301682",
              "301678", "600463", "600396", "600397"]
for c in fail_codes:
    ipo = ipo_map.get(c, "?")
    first = first_map.get(c, "无数据")
    in_2022 = "有" if first and first <= "2022-01-04" else ("无(首日%s)" % first)
    print(f"{c}: ipo={ipo} 首日={first} 2022前有数据={in_2022}", flush=True)
