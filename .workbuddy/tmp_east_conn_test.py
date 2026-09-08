# -*- coding: utf-8 -*-
"""东财连通性诊断：主域/镜像域、带UA/不带UA"""
import os
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
import urllib.request
urllib.request.getproxies = lambda: {}
import requests.utils
requests.utils.get_environ_proxies = lambda *a, **k: {}
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

tests = [
    ("push2his 主域+UA", "https://push2his.eastmoney.com/api/qt/stock/kline/get?fields1=f1&fields2=f51&ut=7eea3edcaed734bea9cbfc24409ed989&klt=5&fqt=0&secid=1.600000&beg=20210101&end=20210110&lmt=1000000", UA),
    ("push2his 主域无UA", "https://push2his.eastmoney.com/api/qt/stock/kline/get?fields1=f1&fields2=f51&ut=7eea3edcaed734bea9cbfc24409ed989&klt=5&fqt=0&secid=1.600000&beg=20210101&end=20210110&lmt=1000000", None),
    ("86 镜像+UA", "http://86.push2his.eastmoney.com/api/qt/stock/kline/get?fields1=f1&fields2=f51&ut=7eea3edcaed734bea9cbfc24409ed989&klt=5&fqt=0&secid=1.600000&beg=20210101&end=20210110&lmt=1000000", UA),
    ("quote.eastmoney", "https://quote.eastmoney.com/", UA),
]
for name, url, headers in tests:
    try:
        r = requests.get(url, timeout=10, headers=headers)
        body = r.text[:200].replace("\n", " ")
        print(f"[OK] {name}: {r.status_code}, {body[:150]}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
