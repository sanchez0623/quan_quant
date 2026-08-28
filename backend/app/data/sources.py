# -*- coding: utf-8 -*-
"""数据源抽象与适配器：baostock / akshare / mootdx 均为可选依赖，
未安装时 available()=False，系统照常运行（可用合成演示数据）。
注意：所有自建 httpx/requests session 一律 trust_env=False（设计文档 4.6）。
"""
import threading
import time
from typing import Callable, Optional

import polars as pl


class DataSource:
    """数据源抽象基类"""
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def health_check(self, timeout: float = 10) -> bool:
        raise NotImplementedError

    def get_daily(self, code: str, start: str, end: str) -> Optional[pl.DataFrame]:
        """返回列: code,date,open,high,low,close,volume,amount"""
        raise NotImplementedError

    def get_minute5(self, code: str, start: str, end: str) -> Optional[pl.DataFrame]:
        raise NotImplementedError

    def get_adj_factor(self, code: str) -> Optional[pl.DataFrame]:
        """返回列: code,date,adj_factor（后复权累计因子；可为事件级，由 updater 展开到每日）"""
        raise NotImplementedError


def _no_session_proxies():
    """构造不走系统代理的 requests session（若 requests 可用）"""
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        return s
    except ImportError:
        return None


def _norm_code(code: str) -> str:
    """归一化为纯 6 位数字代码（系统统一存储格式）。
    兼容 sh.600000 / sh600000 / 600000.SH / 600313.SH / 600000 等输入；无法归一化时原样返回。"""
    code = str(code).strip()
    lower = code.lower()
    if lower.startswith(("sh.", "sz.", "bj.")):   # sh.600000 -> 600000
        return code[3:]
    if lower.startswith(("sh", "sz", "bj")):      # sh600000 -> 600000
        return code[2:]
    if "." in code:                               # 600313.SH -> 600313（代码在前）
        head, _, tail = code.partition(".")
        return head if head.isdigit() else tail
    return code


def _bs_code(code: str) -> Optional[str]:
    """转换为 baostock 9位代码格式：sh.600000 / sz.000001
    兼容已带前缀的输入（sh.600000 / sh600000）；
    科创板(688/689)、北交所(4/8/9开头) baostock 不支持返回 None"""
    code = str(code).strip()
    # 先提取纯数字部分进行判断
    pure_code = code
    if "." in code:
        pure_code = code.split(".", 1)[1]
    elif code.startswith(("sh", "sz", "bj")):
        pure_code = code[2:]
    
    # 科创板(688/689)、北交所(4/8/9开头) baostock 不支持
    if pure_code.startswith("688") or pure_code.startswith("689"):
        return None
    if pure_code.startswith(("4", "8", "9")):
        return None
    
    # 重新构建带前缀的格式
    if "." in code:            # sh.600000
        return code
    if code.startswith(("sh", "sz", "bj")):  # sh600000 -> sh.600000
        return f"{code[:2]}.{code[2:]}"
    if pure_code.startswith("6"):   # 沪市主板（排除科创板688/689）
        return f"sh.{pure_code}"
    if pure_code.startswith(("0", "3")):  # 深市主板 + 创业板300/301
        return f"sz.{pure_code}"
    return None


# ---------------- 板块推导（零数据依赖，代码前缀） ----------------
BOARD_MAIN = "main"       # 主板：60/00
BOARD_CHINEXT = "chinext" # 创业板：30
BOARD_STAR = "star"       # 科创板：688/689
BOARD_BSE = "bse"         # 北交所：4/8/9


def derive_board(code: str) -> Optional[str]:
    """由代码前缀推导所属板块：主板/创业板/科创板/北交所；无法识别返回 None。"""
    c = str(code).strip()
    if "." in c:
        c = c.split(".", 1)[1]
    if c[:2].lower() in ("sh", "sz", "bj"):
        c = c[2:]
    if c.startswith(("688", "689")):
        return BOARD_STAR
    if c.startswith("30"):
        return BOARD_CHINEXT
    if c.startswith(("4", "8", "9")):
        return BOARD_BSE
    if c.startswith(("60", "00")):
        return BOARD_MAIN
    return None


BOARD_LABELS = {
    BOARD_MAIN: "主板",
    BOARD_CHINEXT: "创业板",
    BOARD_STAR: "科创板",
    BOARD_BSE: "北交所",
}


# ---------------- 指数成分（baostock） ----------------
# index_key -> (baostock 查询函数名, 指数中文名)
INDEX_REGISTRY = {
    "sz50": ("query_sz50_stocks", "上证50"),
    "hs300": ("query_hs300_stocks", "沪深300"),
    "zz500": ("query_zz500_stocks", "中证500"),
}
# 派生指数：中证800 = 沪深300 + 中证500 合并
INDEX_CSI800 = "csi800"
INDEX_CSI800_NAME = "中证800"
# 依赖映射（派生指数由哪些基础指数组成）
INDEX_PARENTS = {INDEX_CSI800: ["hs300", "zz500"]}


class BaostockSource(DataSource):
    """日线主源 / 复权因子源 / 5分钟线深历史源"""
    name = "baostock"
    role = "daily主源"

    # baostock 非线程安全：进程内所有访问（登录/查询/登出）用可重入锁串行。
    # 登录态缓存复用，避免每次查询 login/logout 开销；会话失效时自动重登一次。
    _bs_lock = threading.RLock()
    _bs_logged_in = False

    def __init__(self):
        try:
            import baostock as bs  # noqa: F401
            self._bs = bs
            self._ok = True
        except ImportError:
            self._bs = None
            self._ok = False

    def available(self) -> bool:
        return self._ok

    def _ensure_login(self) -> bool:
        """确保已登录（登录态跨查询复用，仅首次真正 login）"""
        with self._bs_lock:
            if self._bs_logged_in:
                return True
            try:
                rs = self._bs.login()
                ok = rs.error_code == "0"
                self._bs_logged_in = ok
                if not ok:
                    try:
                        self._bs.logout()
                    except Exception:
                        pass
                return ok
            except Exception:
                self._bs_logged_in = False
                return False

    def _force_logout(self) -> None:
        """登出并清登录态（会话失效时重置，下次查询自动重登）"""
        with self._bs_lock:
            try:
                self._bs.logout()
            except Exception:
                pass
            self._bs_logged_in = False

    def _run_query(self, qfn) -> Optional[list]:
        """在缓存登录态下执行查询；会话失效（错误码或异常）时登出重登重试一次。
        qfn: () -> (rs, rows)。成功返回 rows（可能为空列表），失败返回 None。"""
        with self._bs_lock:
            try:
                if not self._bs_logged_in and not self._ensure_login():
                    return None
                rs, rows = qfn()
                if rs.error_code == "0":
                    return rows
                # 查询级失败：会话可能被服务端断开 -> 登出重登一次
                self._force_logout()
                if not self._ensure_login():
                    return None
                rs, rows = qfn()
                return rows if rs.error_code == "0" else None
            except Exception:
                self._force_logout()
                return None

    def health_check(self, timeout: float = 10) -> bool:
        if not self._ok:
            return False
        try:
            # 用一次真实小查询验证缓存会话可用（同时负责建立登录）
            def _q():
                rs = self._bs.query_history_k_data_plus(
                    "sh.600000", "date,close",
                    start_date="2020-01-01", end_date="2020-01-10",
                    frequency="d", adjustflag="3")
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                return rs, rows
            return bool(self._run_query(_q))
        except Exception:
            return False

    def get_daily(self, code, start, end):
        if not self._ok:
            return None
        code = _norm_code(code)          # 存储/传递统一纯数字
        bs_code = _bs_code(code)
        if bs_code is None:
            return None
        def _q():
            rs = self._bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start, end_date=end, frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            return rs, rows
        rows = self._run_query(_q)
        if not rows:
            return None
        df = pl.DataFrame(rows, schema={"date": str, "open": str, "high": str,
                                        "low": str, "close": str, "volume": str,
                                        "amount": str}, orient="row")
        df = df.with_columns([
            # 停牌日 baostock 可能返回空串：非严格转数值（空串→null），并剔除无有效收盘价的行
            pl.col("open").cast(pl.Float64, strict=False),
            pl.col("high").cast(pl.Float64, strict=False),
            pl.col("low").cast(pl.Float64, strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
            pl.col("volume").cast(pl.Float64, strict=False),
            pl.col("amount").cast(pl.Float64, strict=False),
            pl.lit(code).alias("code"),
        ]).filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        return df.select(["code", "date", "open", "high", "low", "close", "volume", "amount"])

    def get_adj_factor(self, code, start: str = "1990-01-01",
                       end: str = "2099-12-31") -> Optional[pl.DataFrame]:
        """后复权累计因子（事件级，仅除权除息日；updater 负责展开到每日）。

        使用 query_adjust_factor 的 backAdjustFactor（上市首日=1，向后累计）。
        返回列: code,date(除权除息日),adj_factor；无除权事件时返回单行 1.0 占位。
        必须从上市日起查全量历史，否则早于首个返回事件的日期无法取到正确累计因子。
        """
        if not self._ok:
            return None
        code = _norm_code(code)
        bs_code = _bs_code(code)
        if bs_code is None:
            return None
        def _q():
            rs = self._bs.query_adjust_factor(
                code=bs_code, start_date=start, end_date=end)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            return rs, (rs.fields, rows)
        res = self._run_query(_q)
        if res is None:
            return None
        fields, rows = res
        if not rows:
            # 上市以来无除权除息 -> 因子恒为 1.0（占位行，bisect 展开后每日=1.0）
            return pl.DataFrame(
                {"code": [code], "date": [start], "adj_factor": [1.0]})
        i_date = fields.index("dividOperateDate")
        i_factor = fields.index("backAdjustFactor")
        out = []
        for r in rows:
            try:
                f = float(r[i_factor])
            except (ValueError, TypeError):
                continue
            if f > 0 and r[i_date]:
                out.append({"code": code, "date": r[i_date], "adj_factor": f})
        if not out:
            return pl.DataFrame(
                {"code": [code], "date": [start], "adj_factor": [1.0]})
        return pl.DataFrame(out).unique(subset=["date"]).sort("date")

    def get_index_constituents(self, index_key: str) -> Optional[list[dict]]:
        """拉取指定指数成分（baostock 官方接口，秒级）。

        index_key: sz50 | hs300 | zz500（INDEX_REGISTRY）。
        返回 [{code(纯数字), name, update_date}]；失败返回 None。
        """
        entry = INDEX_REGISTRY.get(index_key)
        if not self._ok or entry is None:
            return None
        qfn = getattr(self._bs, entry[0])

        def _q():
            rs = qfn()
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            return rs, (rs.fields, rows)

        res = self._run_query(_q)
        if res is None:
            return None
        fields, rows = res
        if not rows:
            return None
        i_code = fields.index("code")
        i_name = fields.index("code_name")
        i_date = fields.index("updateDate")
        out = []
        for r in rows:
            code = _norm_code(r[i_code]) if i_code < len(r) else ""
            if not code:
                continue
            out.append({
                "code": code,
                "name": r[i_name] if i_name < len(r) else "",
                "update_date": r[i_date] if i_date < len(r) else "",
            })
        return out or None

    def get_minute5(self, code, start, end):
        """5分钟K线（frequency="5"，支持任意日期区间，历史可追溯至 2015 年）。
        返回列: code,date(YYYY-MM-DD HH:MM),open,high,low,close,volume,amount（不复权）"""
        if not self._ok:
            return None
        code = _norm_code(code)
        bs_code = _bs_code(code)
        if bs_code is None:
            return None
        def _q():
            rs = self._bs.query_history_k_data_plus(
                bs_code,
                "date,time,open,high,low,close,volume,amount",
                start_date=start, end_date=end,
                frequency="5", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            return rs, rows
        rows = self._run_query(_q)
        if not rows:
            return None
        df = pl.DataFrame(
            rows,
            schema={"date": pl.Utf8, "time": pl.Utf8, "open": pl.Utf8,
                    "high": pl.Utf8, "low": pl.Utf8, "close": pl.Utf8,
                    "volume": pl.Utf8, "amount": pl.Utf8},
            orient="row")
        # baostock 分钟线 time 形如 "20250102093500000"（>=12位时取 [8:12] 为 HHMM）；
        # 个别版本仅返回 "093500" 等短格式，取前 4 位为 HHMM。
        hh = pl.when(pl.col("time").str.len_chars() >= 12) \
            .then(pl.col("time").str.slice(8, 2)).otherwise(pl.col("time").str.slice(0, 2))
        mm = pl.when(pl.col("time").str.len_chars() >= 12) \
            .then(pl.col("time").str.slice(10, 2)).otherwise(pl.col("time").str.slice(2, 2))
        df = df.with_columns([
            # 非严格转换：停牌/空值行为空串时转 null，下方过滤会剔除
            pl.col("open").cast(pl.Float64, strict=False),
            pl.col("high").cast(pl.Float64, strict=False),
            pl.col("low").cast(pl.Float64, strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
            pl.col("volume").cast(pl.Float64, strict=False),
            pl.col("amount").cast(pl.Float64, strict=False),
            pl.concat_str([pl.col("date"), pl.lit(" "), hh, pl.lit(":"), mm])
            .alias("date"),
            pl.lit(code).alias("code"),
        ]).select(["code", "date", "open", "high", "low", "close", "volume", "amount"])
        # 剔除停牌/空价行，并按请求窗口再过滤一次
        df = df.filter((pl.col("close") > 0) & (pl.col("volume") >= 0))
        df = df.filter((pl.col("date") >= start)
                       & (pl.col("date") <= end + " 23:59"))
        return df if df.height else None


class AkshareSource(DataSource):
    """日线备源 / 复权因子备源"""
    name = "akshare"
    role = "daily备源"

    def __init__(self):
        try:
            import akshare  # noqa: F401
            self._ak = akshare
            self._ok = True
        except ImportError:
            self._ak = None
            self._ok = False

    def available(self) -> bool:
        return self._ok

    def health_check(self, timeout: float = 10) -> bool:
        return self._ok  # 已安装即视为可用（真实拉数失败自动降级）

    def get_daily(self, code, start, end):
        if not self._ok:
            return None
        code = _norm_code(code)          # akshare 需要 6 位纯数字
        try:
            df = self._ak.stock_zh_a_hist(symbol=code, period="daily",
                                          start_date=start.replace("-", ""),
                                          end_date=end.replace("-", ""), adjust="")
            if df is None or df.empty:
                return None
            import pandas as pd
            pdf = pd.DataFrame(df)
            return pl.from_pandas(pdf[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]]).rename({
                "日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
                "成交量": "volume", "成交额": "amount"}).with_columns(
                pl.lit(code).alias("code"),
                pl.col("date").cast(pl.Utf8),
                # 东财停牌日可能返回空串/异常值：非严格转数值（空串→null），与其它源 schema 一致
                pl.col("volume").cast(pl.Float64, strict=False),
                pl.col("amount").cast(pl.Float64, strict=False),
            ).select(["code", "date", "open", "high", "low", "close", "volume", "amount"])
        except Exception:
            return None

    def get_adj_factor(self, code, start: str = "19900101",
                       end: str = "20991231") -> Optional[pl.DataFrame]:
        """后复权累计因子（日级）：同一接口分别拉不复权与后复权收盘，factor=hfq/raw。

        与 baostock 涨跌幅复权法可能存在微小平台差异，但自洽：
        raw * factor == hfq，datafeed 乘回后得到 akshare 口径后复权价。
        """
        if not self._ok:
            return None
        code = _norm_code(code)
        try:
            import pandas as pd
            s = start.replace("-", "")
            e = end.replace("-", "")
            raw = self._ak.stock_zh_a_hist(symbol=code, period="daily",
                                           start_date=s, end_date=e, adjust="")
            hfq = self._ak.stock_zh_a_hist(symbol=code, period="daily",
                                           start_date=s, end_date=e, adjust="hfq")
            if raw is None or raw.empty or hfq is None or hfq.empty:
                return None
            raw_pdf = pd.DataFrame(raw)
            hfq_pdf = pd.DataFrame(hfq)
            raw_df = pl.from_pandas(raw_pdf).select([
                pl.col("日期").cast(pl.Utf8).alias("date"),
                pl.col("收盘").cast(pl.Float64).alias("raw_close")])
            hfq_df = pl.from_pandas(hfq_pdf).select([
                pl.col("日期").cast(pl.Utf8).alias("date"),
                pl.col("收盘").cast(pl.Float64).alias("hfq_close")])
            df = raw_df.join(hfq_df, on="date", how="inner").with_columns([
                pl.lit(code).alias("code"),
                (pl.col("hfq_close") / pl.col("raw_close")).alias("adj_factor"),
            ]).select(["code", "date", "adj_factor"])
            df = df.filter(pl.col("adj_factor") > 0).sort("date")
            return df if df.height else None
        except Exception:
            return None

    def get_minute5(self, code, start, end):
        """5分钟K线（东财接口，支持科创板/创业板；仅保留约 2 年历史，且受系统代理影响）。
        返回列: code,date(YYYY-MM-DD HH:MM),open,high,low,close,volume,amount（不复权）"""
        if not self._ok:
            return None
        code = _norm_code(code)
        try:
            df = self._ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=start + " 09:30:00",
                end_date=end + " 15:00:00",
                period="5", adjust="")
            if df is None or len(df) == 0:
                return None
            import pandas as pd
            pdf = pd.DataFrame(df)
            return pl.from_pandas(pdf).select([
                pl.lit(code).alias("code"),
                pl.col("时间").cast(pl.Utf8).str.slice(0, 16).alias("date"),
                pl.col("开盘").cast(pl.Float64).alias("open"),
                pl.col("最高").cast(pl.Float64).alias("high"),
                pl.col("最低").cast(pl.Float64).alias("low"),
                pl.col("收盘").cast(pl.Float64).alias("close"),
                pl.col("成交量").cast(pl.Float64).alias("volume"),
                pl.col("成交额").cast(pl.Float64).alias("amount"),
            ])
        except Exception:
            return None


class MootdxSource(DataSource):
    """通达信源：5分钟线主源 + 科创板(688/689)日线/5分钟线主源
    （baostock 不支持科创板，由本源的直接 TCP 连接补上，不受系统代理影响）。"""
    name = "mootdx"
    role = "minute5主源 + 科创板日线主源"

    # TDX 协议单次请求最多返回 800 根 K 线（mootdx 内部也会 clamp 到 800）。
    # 分页按 800 步进回捞：5 分钟线 ≈ 2 年历史（服务器端深度限制），日线可至全历史。
    PAGE_SIZE = 800
    MAX_PAGES = 300   # 300*800=24 万根，足够覆盖服务器端全部可回捞深度

    def __init__(self):
        try:
            from mootdx.quotes import Quotes  # noqa: F401
            self._client = None
            self._ok = True
        except ImportError:
            self._ok = False

    def available(self) -> bool:
        return self._ok

    def _get_client(self):
        if self._client is None:
            from mootdx.quotes import Quotes
            self._client = Quotes.factory(market="std", timeouts=(10, 10))  # 不走代理
        return self._client

    def health_check(self, timeout: float = 10) -> bool:
        if not self._ok:
            return False
        try:
            # 旧版 mootdx 的 stocks() 不支持 code 参数会抛 TypeError，
            # 改用单次 K 线请求验证（也是实际取数路径，快且稳定）
            return self._get_client().bars(symbol="600000", frequency=9, offset=1) is not None
        except Exception:
            return False

    def _fetch_bars(self, code, frequency, start, end):
        """分页拉取通达信 K 线并归一化为 code,date,open,high,low,close,volume,amount。

        TDX 每次最多返回 800 根（PAGE_SIZE），start=0 为最新，按 PAGE_SIZE 步进回捞；
        分页边界可能重叠，按 datetime 列去重。frequency: 0=5分钟, 9=日线。
        """
        client = self._get_client()
        import pandas as pd
        pages = []
        pos = 0
        for _ in range(self.MAX_PAGES):
            try:
                df = client.bars(symbol=code, frequency=frequency,
                                 offset=self.PAGE_SIZE, start=pos)
            except TypeError:
                # 旧版 mootdx 不支持 start 分页：退化为单次拉取
                df = client.bars(symbol=code, frequency=frequency, offset=self.PAGE_SIZE)
                pages.append(df)
                break
            if df is None or len(df) == 0:
                break
            pages.append(df)
            if len(df) < self.PAGE_SIZE:
                break  # 已到历史尽头
            # 本页最早日期已早于请求起始日，无需再翻更老的页
            try:
                earliest = str(df["datetime"].min())
            except Exception:
                earliest = None
            if earliest and start and earliest[:10] < start[:10]:
                break
            pos += self.PAGE_SIZE
        if not pages:
            return None
        pdf = pd.concat(pages).reset_index(drop=True)  # 丢弃与 datetime 列重复的索引
        # 分页边界可能重叠：以 datetime 列为准去重/排序（保留最近拉取的）
        pdf = pdf.drop_duplicates(subset="datetime", keep="last").sort_values("datetime")
        out = pl.from_pandas(pdf).with_columns(pl.lit(code).alias("code"))
        date_slice = 10 if frequency == 9 else 16  # 日线 YYYY-MM-DD；5分钟 YYYY-MM-DD HH:MM
        out = out.select([
            "code",
            pl.col("datetime").cast(pl.Utf8).str.slice(0, date_slice).alias("date"),
            "open", "high", "low", "close",
            pl.col("volume").cast(pl.Float64),
            pl.col("amount").cast(pl.Float64),
        ])
        out = out.filter((pl.col("close") > 0)
                         & (pl.col("date") >= start) & (pl.col("date") <= end + " 23:59"))
        return out if out.height else None

    def get_minute5(self, code, start, end):
        """5分钟K线（frequency=0，支持科创板；服务器端深度约 2 年）。
        返回列: code,date(YYYY-MM-DD HH:MM),open,high,low,close,volume,amount（不复权）"""
        if not self._ok:
            return None
        code = _norm_code(code)
        try:
            return self._fetch_bars(code, 0, start, end)
        except Exception:
            return None

    def get_daily(self, code, start, end):
        """日线K线（frequency=9，支持科创板；可回捞至上市日全历史）。baostock 无科创板数据时作备源。"""
        if not self._ok:
            return None
        code = _norm_code(code)
        try:
            return self._fetch_bars(code, 9, start, end)
        except Exception:
            return None


# 源注册与健康状态缓存（未检查/未安装 → null）
SOURCES: list[DataSource] = [BaostockSource(), AkshareSource(), MootdxSource()]
_health_cache: dict[str, dict] = {}


def health_snapshot() -> list[dict]:
    """返回 sources 健康快照：healthy=null 表示未安装/未检查"""
    out = []
    for s in SOURCES:
        if not s.available():
            out.append({"name": s.name, "role": s.role, "healthy": None,
                        "last_check": None, "note": "未安装，可选依赖"})
        else:
            cached = _health_cache.get(s.name)
            out.append({"name": s.name, "role": s.role,
                        "healthy": cached["healthy"] if cached else None,
                        "last_check": cached["last_check"] if cached else None,
                        "note": ""})
    return out


def check_health(timeout: float = 10,
                 on_each: Optional[Callable[[str, str], None]] = None) -> dict[str, bool]:
    """对已安装源做健康检查并缓存结果；on_each(name, role) 在每个源检查前回调"""
    result = {}
    for s in SOURCES:
        if not s.available():
            continue
        if on_each is not None:
            try:
                on_each(s.name, s.role)
            except Exception:
                pass
        healthy = False
        try:
            healthy = bool(s.health_check(timeout))
        except Exception:
            healthy = False
        result[s.name] = healthy
        _health_cache[s.name] = {"healthy": healthy,
                                 "last_check": time.strftime("%Y-%m-%d %H:%M:%S")}
    return result
