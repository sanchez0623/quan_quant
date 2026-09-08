# -*- coding: utf-8 -*-
"""直接查 baostock 对 302132 的日线与5分钟线原始返回"""
import sys, pathlib
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data.sources import BaostockSource, _bs_code

bs = BaostockSource()
print("_bs_code(302132):", _bs_code("302132"), flush=True)

def q(qfn):
    rows = []
    import time
    rs = None
    # 直接调用底层（不经过 _run_query 的封装，便于看 error_code）
    rs = qfn()
    while rs and rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, rows

# 日线 2023-04 一周
rs, rows = q(lambda: bs._bs.query_history_k_data_plus(
    "sz.302132", "date,open,high,low,close,volume",
    start_date="2023-04-03", end_date="2023-04-07", frequency="d", adjustflag="3"))
print("日线 error_code:", getattr(rs, "error_code", None), "rows:", len(rows), rows[:2], flush=True)

# 5分钟 2023-04 一周
rs, rows = q(lambda: bs._bs.query_history_k_data_plus(
    "sz.302132", "date,time,open,high,low,close,volume",
    start_date="2023-04-03", end_date="2023-04-07", frequency="5", adjustflag="3"))
print("5分钟 error_code:", getattr(rs, "error_code", None), "rows:", len(rows), rows[:2], flush=True)
