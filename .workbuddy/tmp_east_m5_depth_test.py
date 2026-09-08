# -*- coding: utf-8 -*-
"""实测东财(akshare) 5分钟线历史深度：能否到 2021？速度如何？"""
import os, time
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
import urllib.request
urllib.request.getproxies = lambda: {}          # 绕过 Windows 系统代理（注册表）
import requests.utils
requests.utils.get_environ_proxies = lambda *a, **k: {}
import akshare as ak

# 1) 深度探测：600000 拉 2021-01-01~2021-06-30
t0 = time.time()
df = ak.stock_zh_a_hist_min_em(symbol="600000",
                               start_date="2021-01-01 09:30:00",
                               end_date="2021-06-30 15:00:00",
                               period="5", adjust="")
dt = time.time() - t0
if df is None or len(df) == 0:
    print(f"2021 上半年: 无数据（{dt:.1f}s）")
else:
    print(f"2021 上半年: {len(df)} 行, 首={df['时间'].iloc[0]}, 末={df['时间'].iloc[-1]}, 耗时 {dt:.1f}s")
    print("列名:", list(df.columns))
    print(df.head(3).to_string())
    print(df.tail(2).to_string())

# 2) 深度再探：2021 全年
t0 = time.time()
df2 = ak.stock_zh_a_hist_min_em(symbol="600000",
                                start_date="2021-01-01 09:30:00",
                                end_date="2021-12-31 15:00:00",
                                period="5", adjust="")
dt2 = time.time() - t0
if df2 is None or len(df2) == 0:
    print(f"2021 全年: 无数据（{dt2:.1f}s）")
else:
    print(f"2021 全年: {len(df2)} 行, 首={df2['时间'].iloc[0]}, 末={df2['时间'].iloc[-1]}, 耗时 {dt2:.1f}s")
