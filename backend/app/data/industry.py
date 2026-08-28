# -*- coding: utf-8 -*-
"""申万 2021 三级行业管道：行业树（一级/二级/三级）+ 逐三级行业抓取成分股。

数据源：乐咕乐股（legulegu.com），零密钥零成本（方案 §3.2）。
- 行业树：优先 akshare sw_index_first/second/third_info（同底层数据源，已封装）；
  不可用时回退直接解析乐咕 sw-industry-overview 页面。
- 成分股：直接请求乐咕 index-composition 页面并解析 HTML 表格
  （akshare 1.18.60 的 sw_index_third_cons 存在列数不匹配 bug，故自实现）。
- 并发上限 2 + 0.8s 请求间隔（参考项目实测安全参数，不得放宽）。

输出行：(code, sw_l1, sw_l2, sw_l3, sw_code) —— 只取三级行业条目成分，
一二级成分是其超集，避免重复计数。
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from typing import Callable, Optional

import polars as pl

from . import sources

_URL_OVERVIEW = "https://legulegu.com/stockdata/sw-industry-overview"
_URL_COMPOSITION = "https://legulegu.com/stockdata/index-composition?industryCode={code}"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": _URL_OVERVIEW,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 并发与限流参数（方案 §3.2：参考项目实测安全参数，不得放宽）
MAX_CONCURRENCY = 2
REQUEST_INTERVAL = 0.8

_NAME_RE = re.compile(r"^(.*?)\((\d+)\)(?:\[(.*?)\])?\s*$")


class _RateLimiter:
    """全局请求节流：任意两次请求间隔 >= interval（并发下仍按全局串行发请求，防限流）"""

    def __init__(self, interval: float):
        self._interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last = now


def _session():
    """不走系统代理的 requests 会话（项目约定 trust_env=False，设计文档 4.6）"""
    return sources._no_session_proxies()


# ---------------- 行业树 ----------------

def _tree_from_akshare() -> dict:
    """通过 akshare 拉取申万 2021 三级行业树（优先路径，已验证列结构稳定）"""
    import akshare as ak
    d1 = ak.sw_index_first_info()   # 行业代码/行业名称/成份个数
    d2 = ak.sw_index_second_info()  # 上级行业=一级名
    d3 = ak.sw_index_third_info()   # 上级行业=二级名
    l1 = []   # {"code", "name"}
    for r in d1.to_dict("records"):
        code = str(r["行业代码"]).strip()
        name = str(r["行业名称"]).strip()
        if code and name:
            l1.append({"code": code, "name": name})
    l2 = []   # {"code", "name", "parent"}
    for r in d2.to_dict("records"):
        code = str(r["行业代码"]).strip()
        name = str(r["行业名称"]).strip()
        parent = str(r["上级行业"] or "").strip()
        if code and name:
            l2.append({"code": code, "name": name, "parent": parent})
    l3 = []   # {"code", "name", "parent"}
    for r in d3.to_dict("records"):
        code = str(r["行业代码"]).strip()
        name = str(r["行业名称"]).strip()
        parent = str(r["上级行业"] or "").strip()
        if code and name:
            l3.append({"code": code, "name": name, "parent": parent})
    return {"l1": l1, "l2": l2, "l3": l3}


def _parse_overview_section(soup, level: int) -> list[dict]:
    """解析一级/二级/三级行业条目：code 取自 lg-industries-item-chinese-title，
    name 形如 '农林牧渔(104)' 或 '种植业(21)[农林牧渔]'。"""
    container = soup.find("div", attrs={"id": f"level{level}Items"})
    if container is None:
        return []
    out = []
    codes = container.find_all("div", attrs={"class": "lg-industries-item-chinese-title"})
    names = container.find_all("div", attrs={"class": "lg-industries-item-number"})
    for code_el, name_el in zip(codes, names):
        code = (code_el.get_text() or "").strip()
        text = (name_el.get_text() or "").strip()
        m = _NAME_RE.match(text)
        if not m or not code:
            continue
        name, parent = m.group(1).strip(), (m.group(3) or "").strip()
        if name:
            out.append({"code": code, "name": name, "parent": parent})
    return out


def _tree_from_html() -> dict:
    """回退路径：直接解析乐咕 sw-industry-overview 页面（trust_env=False 会话）"""
    import pandas as pd  # noqa: F401  # bs4/lxml 随 akshare 安装
    from bs4 import BeautifulSoup
    s = _session()
    r = s.get(_URL_OVERVIEW, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    return {
        "l1": _parse_overview_section(soup, 1),
        "l2": _parse_overview_section(soup, 2),
        "l3": _parse_overview_section(soup, 3),
    }


def fetch_sw_tree() -> dict:
    """返回 {"l1": [{code,name}], "l2": [{code,name,parent}], "l3": [{code,name,parent}]}。
    akshare 优先，失败回退直接解析乐咕 HTML。全失败抛异常。"""
    try:
        return _tree_from_akshare()
    except Exception:  # noqa: BLE001
        return _tree_from_html()


# ---------------- 成分股（单行业） ----------------

def fetch_sw_constituents(industry_code: str) -> list[dict]:
    """抓取单三级行业成分股。industry_code 形如 '850111.SI'。
    返回 [{code(纯数字), name}]；失败抛异常（由调用方记录并跳过，不阻断整体）。
    """
    import pandas as pd
    s = _session()
    url = _URL_COMPOSITION.format(code=industry_code)
    r = s.get(url, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    table = pd.read_html(StringIO(r.text))[0]
    cols = list(table.columns)
    i_code = next((i for i, c in enumerate(cols) if "代码" in str(c)), None)
    i_name = next((i for i, c in enumerate(cols) if ("简称" in str(c) or "名称" in str(c))), None)
    if i_code is None:
        raise RuntimeError(f"成分页 {industry_code} 未解析到代码列: {cols}")
    out = []
    for r_ in table.itertuples(index=False):
        code = sources._norm_code(str(r_[i_code]))
        name = str(r_[i_name]) if i_name is not None else ""
        if code and code.isdigit():
            out.append({"code": code, "name": name})
    return out or []


# ---------------- 理杏仁加速路径（可选，LIXINGER_API_KEY 存在时启用） ----------------
# 方案 §3.3：2 次请求拿全量（行业信息 + 成分），快百倍；存储格式与乐咕路径一致。
_LIXINGER_BASE = "https://open.lixinger.com/api"
_LIXINGER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _lixinger_to_rows(ind_list: list[dict], cons_data: list[dict]) -> list[dict]:
    """把理杏仁返回（行业信息 + 成分）组装成行。
    只取三级行业成分（避免一二级超集重复）；l1/l2 由六位代码前缀推导。
    返回 [{code, sw_l1, sw_l2, sw_l3, sw_code}]"""
    ind_map = {it.get("stockCode"): it for it in ind_list if it.get("stockCode")}
    rows: list[dict] = []
    for industry in cons_data or []:
        icode = str(industry.get("stockCode") or "")
        meta = ind_map.get(icode)
        if meta is None or meta.get("level") != "three":
            continue  # 只取三级行业
        l1_name = ind_map.get(icode[:2] + "0000", {}).get("name", "")
        l2_name = ind_map.get(icode[:4] + "00", {}).get("name", "")
        l3_name = meta.get("name", "")
        for c in industry.get("constituents") or []:
            code = sources._norm_code(str(c.get("stockCode") or ""))
            if code and code.isdigit():
                rows.append({"code": code, "sw_l1": l1_name, "sw_l2": l2_name,
                             "sw_l3": l3_name, "sw_code": icode})
    return rows


def fetch_sw_industry_lixinger(api_key: str,
                               progress_cb: Optional[Callable[[float, str], None]] = None) -> pl.DataFrame:
    """理杏仁加速路径：2 次请求拿全量申万 2021 三级行业成分（快百倍）。
    返回列与 crawl_sw_industry 一致: code, sw_l1, sw_l2, sw_l3, sw_code, snapshot_date"""
    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    s = _session()
    headers = {"User-Agent": _LIXINGER_UA, "Accept": "application/json"}

    report(15, "理杏仁: 拉取申万2021行业信息（1/2）...")
    r1 = s.post(f"{_LIXINGER_BASE}/cn/industry",
                json={"token": api_key, "source": "sw_2021"}, headers=headers, timeout=60)
    r1.raise_for_status()
    ind_list = (r1.json() or {}).get("data") or []
    if not ind_list:
        raise RuntimeError("理杏仁行业信息接口返回为空")

    report(30, "理杏仁: 拉取行业成分（2/2，date=latest 全量）...")
    r2 = s.post(f"{_LIXINGER_BASE}/cn/industry/constituents/sw_2021",
                json={"token": api_key, "date": "latest"}, headers=headers, timeout=120)
    r2.raise_for_status()
    cons_data = (r2.json() or {}).get("data") or []
    if not cons_data:
        raise RuntimeError("理杏仁成分接口返回为空")

    report(80, "理杏仁: 组装三级行业归属（去重）...")
    rows = _lixinger_to_rows(ind_list, cons_data)
    if not rows:
        raise RuntimeError("理杏仁成分解析为空")
    snapshot = time.strftime("%Y-%m-%d")
    df = pl.DataFrame(rows).unique(subset=["code"], keep="first").sort("code")
    df = df.with_columns(pl.lit(snapshot).alias("snapshot_date"))
    df = df.select(["code", "sw_l1", "sw_l2", "sw_l3", "sw_code", "snapshot_date"])
    report(95, f"理杏仁: 完成，共 {df.height} 只股票 / {df['sw_code'].n_unique()} 个三级行业")
    return df


# ---------------- 全量抓取编排 ----------------

def _build_hierarchy(tree: dict) -> list[dict]:
    """行业树 -> 三级行业列表 [{code, name, sw_l1, sw_l2}]"""
    l1_by_name = {x["name"]: x["code"] for x in tree["l1"]}
    l2_by_name: dict[str, list[dict]] = {}
    for x in tree["l2"]:
        l2_by_name.setdefault(x["name"], []).append(x)
    out = []
    for x in tree["l3"]:
        l2_name = x.get("parent") or ""
        l2_matches = l2_by_name.get(l2_name, [])
        l2_entry = l2_matches[0] if l2_matches else None
        l1_name = l2_entry["parent"] if l2_entry else ""
        out.append({
            "code": x["code"],
            "name": x["name"],
            "sw_l1": l1_name,
            "sw_l2": l2_name,
            "sw_l1_code": l1_by_name.get(l1_name, ""),
        })
    return out


def crawl_sw_industry(
    progress_cb: Optional[Callable[[float, str], None]] = None,
    concurrency: int = MAX_CONCURRENCY,
    interval: float = REQUEST_INTERVAL,
) -> pl.DataFrame:
    """全量抓取申万三级行业成分，返回 stock_industry 长表。

    并发上限 concurrency + 全局 interval 请求间隔；单行业失败记录并跳过（不阻断整体）。
    返回列: code, sw_l1, sw_l2, sw_l3, sw_code, snapshot_date
    """
    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    tree = fetch_sw_tree()
    l3_list = _build_hierarchy(tree)
    total = len(l3_list)
    if not total:
        raise RuntimeError("申万三级行业树为空（数据源不可用）")
    report(5, f"申万三级: 行业树就绪（{total} 个三级行业），开始抓取成分...")

    limiter = _RateLimiter(interval)
    snapshot = time.strftime("%Y-%m-%d")

    def _worker(item: dict) -> Optional[pl.DataFrame]:
        limiter.wait()
        try:
            cons = fetch_sw_constituents(item["code"])
        except Exception as e:  # noqa: BLE001
            return None
        if not cons:
            return None
        return pl.DataFrame({
            "code": [c["code"] for c in cons],
            "sw_l1": [item["sw_l1"]] * len(cons),
            "sw_l2": [item["sw_l2"]] * len(cons),
            "sw_l3": [item["name"]] * len(cons),
            "sw_code": [item["code"]] * len(cons),
            "snapshot_date": [snapshot] * len(cons),
        })

    frames = []
    ok = failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, it): it for it in l3_list}
        for fut in as_completed(futures):
            item = futures[fut]
            done += 1
            try:
                df = fut.result()
            except Exception:  # noqa: BLE001
                df = None
            if df is not None and df.height:
                frames.append(df)
                ok += 1
            else:
                failed += 1
            report(5 + 90 * done / total,
                   f"申万三级: {item['name']} ({done}/{total}) 成功{ok} 跳过{failed}")
    if not frames:
        raise RuntimeError("所有三级行业成分抓取失败（乐咕不可用或反爬），未写入任何数据")
    report(96, f"申万三级: 合并去重（成功 {ok} 行业 / 跳过 {failed}）...")
    df = pl.concat(frames)
    # 一票一行：同 code 只保留首次出现的行业归属
    df = df.unique(subset=["code"], keep="first").sort("code")
    df = df.select(["code", "sw_l1", "sw_l2", "sw_l3", "sw_code", "snapshot_date"])
    return df
