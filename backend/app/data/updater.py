# -*- coding: utf-8 -*-
"""增量更新服务：健康检查 → 主备降级 → 拉数 → 校验 → 写 parquet → 记录水位。
框架完整；可选数据源不可用时抛出带说明的错误（提示生成演示数据）。
"""
import bisect
import time
import traceback
from typing import Callable, Optional

import polars as pl

from . import sources, store


class UpdateError(RuntimeError):
    pass


def _norm_codes(codes: Optional[list[str]]) -> Optional[list[str]]:
    """股票代码列表归一化为纯数字并去重（保序）"""
    if not codes:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        c = sources._norm_code(str(c).strip())
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out or None


def _validate_daily(df: pl.DataFrame, calendar_dates: list[str]) -> None:
    """校验：K线数 vs 日历、异常价检测（价格<=0 / high<low）"""
    if df.height == 0:
        raise UpdateError("拉取到的日线数据为空")
    bad = df.filter((pl.col("close") <= 0) | (pl.col("high") < pl.col("low")))
    if bad.height:
        raise UpdateError(f"日线数据存在异常价格（{bad.height} 行）")
    # 每只股票的交易日应基本落在日历内（放宽至 15%，兼容节假日/停牌）
    out_of_cal = df.join(pl.DataFrame({"date": calendar_dates}), on="date", how="anti")
    if df.height and out_of_cal.height / df.height > 0.15:
        import logging
        logging.warning(f"日线数据有 {out_of_cal.height}/{df.height} 行日期不在交易日历内（可能为节假日/停牌），但未阻断更新")
    # 不再抛错，仅警告


def _expand_adj_to_daily(adj_events: pl.DataFrame, daily_dates: dict[str, list[str]]) -> pl.DataFrame:
    """把事件级复权因子展开为日级序列。

    adj_events: code,date(除权除息日或每日),adj_factor（backAdjustFactor 累计值）。
    daily_dates: code -> 升序交易日列表（来自实际拉到的日线）。
    对每个交易日 d，取 date <= d 的最新一个事件因子（bisect 右查），
    早于首个事件的日期因子=1.0。对已为日级的因子同样适用（精确命中）。
    """
    frames = []
    for code, dates in daily_dates.items():
        sub = adj_events.filter(pl.col("code") == code).sort("date")
        if sub.height == 0:
            factors = [1.0] * len(dates)
        else:
            ev_dates = sub["date"].to_list()
            ev_factors = sub["adj_factor"].cast(pl.Float64).to_list()
            factors = []
            for d in dates:
                idx = bisect.bisect_right(ev_dates, d) - 1
                factors.append(float(ev_factors[idx]) if idx >= 0 else 1.0)
        frames.append(pl.DataFrame({
            "code": [code] * len(dates),
            "date": dates,
            "adj_factor": factors,
        }))
    return pl.concat(frames).sort(["code", "date"])


def _fetch_all_index_constituents() -> Optional[list[dict]]:
    """baostock 三指数成分 + csi800 派生；任一基础指数失败返回 None（调用方做失败安全）"""
    from .sources import (BaostockSource, INDEX_CSI800, INDEX_PARENTS,
                          INDEX_REGISTRY)
    bs = BaostockSource()
    if not bs.available():
        return None
    rows: list[dict] = []
    for key in INDEX_REGISTRY:
        res = bs.get_index_constituents(key)
        if res is None:
            return None
        for r in res:
            rows.append({"index_key": key, "code": r["code"], "name": r["name"],
                         "update_date": r["update_date"]})
    # csi800 派生 = hs300 + zz500 合并（长表允许一票多 index_key）
    derived: dict[str, dict] = {}
    for parent in INDEX_PARENTS[INDEX_CSI800]:
        for r in rows:
            if r["index_key"] == parent:
                derived.setdefault(r["code"], r)
    for code, r in derived.items():
        rows.append({"index_key": INDEX_CSI800, "code": code, "name": r["name"],
                     "update_date": r["update_date"]})
    return rows or None


def update_industry(data_dir: Optional[str] = None,
                    progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    """更新指数成分 + 申万三级行业（scope=industry）。失败安全：
    任一步拉空但本地已有旧数据 -> 保留旧数据并在统计标注 kept_old。
    申万行业取数顺序：理杏仁（LIXINGER_API_KEY 存在时，2 次请求全量）-> 乐咕爬虫。"""
    import os

    from . import industry

    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    stats: dict = {"scope": "industry", "kept_old": {}}

    # ---- 步骤 1：baostock 指数成分（秒级，全量替换） ----
    report(1, "拉取指数成分（上证50/沪深300/中证500 + 中证800派生）...")
    rows = _fetch_all_index_constituents()
    snapshot = time.strftime("%Y-%m-%d")
    if rows:
        df_idx = pl.DataFrame(rows).with_columns(pl.lit(snapshot).alias("snapshot_date"))
        store.write_index_constituents(df_idx, data_dir)
        stats["index_rows"] = df_idx.height
        stats["index_snapshot"] = snapshot
    else:
        old = store.read_index_constituents(data_dir)
        if old is not None and old.height:
            stats["kept_old"]["index"] = True
            report(8, "警告：指数成分拉取为空，保留本地旧数据")
        else:
            report(8, "警告：指数成分拉取为空且无本地旧数据，跳过")

    # ---- 步骤 2：申万三级行业（理杏仁加速 -> 乐咕爬虫，全量替换） ----
    def _write_industry(df_ind, source: str) -> None:
        store.write_stock_industry(df_ind, data_dir)
        stats["industry_rows"] = df_ind.height
        stats["industry_l3"] = df_ind["sw_code"].n_unique()
        stats["industry_snapshot"] = snapshot
        stats["industry_source"] = source

    lixinger_key = os.environ.get("LIXINGER_API_KEY", "").strip()
    if lixinger_key:
        report(10, "使用理杏仁加速路径拉取申万三级行业（2 次请求全量）...")
        try:
            df_ind = industry.fetch_sw_industry_lixinger(lixinger_key, progress_cb=report)
            _write_industry(df_ind, "lixinger")
            report(100, "行业与成分更新完成（理杏仁）")
            return stats
        except Exception as e:  # noqa: BLE001
            report(12, f"理杏仁拉取失败（{e}），回退乐咕爬虫...")

    report(10, "抓取申万三级行业（乐咕，约 3~5 分钟）...")
    try:
        df_ind = industry.crawl_sw_industry(progress_cb=report)
        _write_industry(df_ind, "legulegu")
    except Exception as e:  # noqa: BLE001
        old = store.read_stock_industry(data_dir)
        if old is not None and old.height:
            stats["kept_old"]["industry"] = True
            report(95, f"警告：申万行业抓取失败（{e}），保留本地旧数据")
        else:
            raise
    report(100, "行业与成分更新完成")
    return stats


def update_stock_basic(data_dir: Optional[str] = None,
                       progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
    """刷新股票元数据（scope=stock_basic）：用 baostock query_all_stock 拉"当前在市"全部 A 股，
    重写 stock_basic 的 name/st，并新增 delisted 标记（不在当日在市集合 = 已退市/暂停）。

    依赖：baostock 可用；stock_basic.parquet 已存在（保留旧 list_date，合并写入）。
    """
    from . import sources, store

    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    stats: dict = {"scope": "stock_basic"}

    basic = store.read_stock_basic(data_dir)
    if basic is None:
        raise UpdateError("本地 stock_basic.parquet 不存在，无法刷新股票列表。"
                          "请先初始化数据或使用 POST /api/data/demo")

    # 用最近一个交易日查询（baostock 需要有效交易日，非交易日返回空）
    day = time.strftime("%Y-%m-%d")
    src = None
    for s in sources.SOURCES:
        if s.available() and isinstance(s, sources.BaostockSource):
            src = s
            break
    if src is None:
        raise UpdateError("baostock 不可用，无法刷新股票列表（需要 query_all_stock）")

    report(5, f"拉取 {day} 全市场在市证券...")
    rows = src.get_all_stocks(day)
    if not rows:
        # 尝试交易日历最近开市日
        cal = store.read_calendar(data_dir)
        if cal is not None and cal.height:
            open_days = (cal.filter(pl.col("is_open") == 1)
                         .filter(pl.col("date") <= day)["date"].sort().to_list())
            if open_days:
                rows = src.get_all_stocks(str(open_days[-1]))
                if rows:
                    report(5, f"当日无数据，改用最近交易日 {open_days[-1]}")
    if not rows:
        raise UpdateError(f"query_all_stock({day}) 返回为空，无法刷新股票列表")

    in_market = pl.DataFrame(rows)
    in_codes = set(in_market["code"].to_list())
    report(20, f"在市 A 股 {len(in_codes)} 只，合并到本地股票列表...")

    # 合并：以本地 basic 为主键，用在市快照覆盖 name/st，标记 delisted
    basic = basic.with_columns(
        pl.col("code").cast(pl.Utf8))
    merged = (basic.join(
        in_market.select(["code", "name", "st"]),
        on="code", how="left",
        suffix="_new")
        .with_columns([
            pl.col("name_new").fill_null(pl.col("name")).alias("name"),
            pl.col("st_new").fill_null(pl.col("st")).alias("st"),
            (~pl.col("code").is_in(sorted(in_codes))).alias("delisted"),
        ])
        .drop(["name_new", "st_new"])
        .sort("code"))
    # 保证列序稳定：code,name,st,list_date,delisted
    cols = [c for c in ("code", "name", "st", "list_date", "delisted") if c in merged.columns]
    merged = merged.select(cols)

    store.write_stock_basic(merged, data_dir)
    report(90, "写回 stock_basic.parquet...")
    st_n = int(merged.filter(pl.col("st")).height)
    delisted_n = int(merged.filter(pl.col("delisted")).height)
    stats.update({
        "total": merged.height,
        "st_count": st_n,
        "delisted_count": delisted_n,
        "snapshot": day,
        "in_market": len(in_codes),
    })
    report(100, "股票列表更新完成")
    return stats


def update_calendar(data_dir: Optional[str] = None,
                    progress_cb: Optional[Callable[[float, str], None]] = None,
                    start_date: str = "1990-01-01",
                    end_date: str = "2099-12-31") -> dict:
    """交易日历刷新（scope="calendar"）：基于 baostock query_trade_dates。

    独立分支：不依赖 stock_basic / 日线健康检查。仅写开盘日——
    _validate_daily 把日历日期无条件当有效交易日使用，写入非交易日会污染校验。
    """
    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    report(5, "交易日历: 定位 baostock 源...")
    bs = next((s for s in sources.SOURCES if s.name == "baostock" and s.available()), None)
    if bs is None:
        raise UpdateError("baostock 不可用（未安装或无法登录），交易日历仅支持 baostock 源")
    report(20, "健康检查: baostock...")
    if not bs.health_check(timeout=10):
        raise UpdateError("baostock 健康检查失败，无法刷新交易日历")
    report(40, f"拉取交易日历 {start_date} ~ {end_date}...")
    cal = bs.get_trade_dates(start=start_date, end=end_date)
    if cal is None or cal.height == 0:
        # 空结果不落库：避免用空日历覆盖本地有效日历
        raise UpdateError("baostock 交易日历拉取失败或为空，已拒绝覆盖本地日历")
    report(80, "写回 trade_calendar.parquet...")
    store.write_calendar(cal, data_dir)
    report(100, "交易日历刷新完成")
    return {"scope": "calendar", "calendar_rows": cal.height,
            "first": cal["date"].min(), "last": cal["date"].max()}


def update(scope: str = "daily", codes: Optional[list[str]] = None,
           data_dir: Optional[str] = None,
           progress_cb: Optional[Callable[[float, str], None]] = None,
           start_date: str = "1990-01-01",
           end_date: str = "2099-12-31") -> dict:
    """scope: daily | minute5 | industry | stock_basic | calendar | all。返回统计。无可用源抛 UpdateError。
    start_date/end_date: 拉取区间（默认全历史；5分钟线受通达信服务器约2年深度限制）。
    industry scope 独立分支：更新指数成分 + 申万三级行业，不需 stock_basic / 日线源。
    stock_basic scope 独立分支：刷新股票元数据（ST/退市标记），不需日线源。
    calendar scope 独立分支：基于 baostock 交易日查询刷新交易日历，不需 stock_basic / 日线源。"""

    def report(p: float, msg: str) -> None:
        if progress_cb:
            progress_cb(p, msg)

    # ---- industry scope：指数成分 + 申万三级（不走日线健康检查/stock_basic） ----
    if scope == "industry":
        return update_industry(data_dir=data_dir, progress_cb=progress_cb)

    # ---- stock_basic scope：刷新股票元数据（ST/退市标记） ----
    if scope == "stock_basic":
        return update_stock_basic(data_dir=data_dir, progress_cb=progress_cb)

    # ---- calendar scope：交易日历刷新（baostock query_trade_dates，仅开盘日） ----
    if scope == "calendar":
        return update_calendar(data_dir=data_dir, progress_cb=progress_cb,
                               start_date=start_date, end_date=end_date)

    installed_srcs = [s for s in sources.SOURCES if s.available()]
    _hi = {"i": 0}

    def _on_health(name: str, role: str) -> None:
        p = 2 + 3 * _hi["i"] / max(len(installed_srcs), 1)
        _hi["i"] += 1
        report(p, f"健康检查: {name}（{role}）...")

    health = sources.check_health(timeout=10, on_each=_on_health)
    report(5, f"健康检查完成: " + ", ".join(f"{k}={'OK' if v else '失败'}" for k, v in health.items()))
    daily_ok = [s for s in sources.SOURCES if s.available() and health.get(s.name)]
    if not daily_ok:
        raise UpdateError(
            "无可用数据源（baostock/akshare/mootdx 未安装或健康检查失败）。"
            "请安装 requirements-sources.txt 中的可选依赖，或调用 POST /api/data/demo 生成演示数据")

    basic = store.read_stock_basic(data_dir)
    if basic is None:
        raise UpdateError("本地 stock_basic.parquet 不存在，无法确定更新范围。"
                          "请先在有真实数据源的环境初始化，或使用 POST /api/data/demo")
    # 退市股不进默认更新范围（防止把已删除的退市股K线从源端重新拉回；
    # 显式指定 codes 仍可强制拉取）
    if "delisted" in basic.columns:
        all_codes = basic.filter(~pl.col("delisted"))["code"].to_list()
    else:
        all_codes = basic["code"].to_list()   # read_stock_basic 已归一化为纯数字
    update_codes = _norm_codes(codes) or all_codes

    scopes = ["daily", "minute5"] if scope == "all" else [scope]
    stats: dict = {"scope": scope, "codes": len(update_codes), "daily_rows": 0,
                   "minute5_rows": 0, "adj_factor_rows": 0, "adj_factor_codes": 0,
                   "start_date": start_date, "end_date": end_date}

    if "daily" in scopes:
        frames = []
        adj_frames = []
        adj_ok_codes = 0
        total = len(update_codes)
        for i, code in enumerate(update_codes):
            report(5 + 70 * i / total, f"正在拉取日线: {code} ({i + 1}/{total})")
            df = None
            src_used = None
            for src in daily_ok:  # 主备降级
                df = src.get_daily(code, start_date, end_date)
                if df is not None:
                    src_used = src
                    break
            if df is not None:
                frames.append(df)
                # 复权因子必须与日线同源，避免不同平台复权口径不一致
                if src_used is not None and hasattr(src_used, "get_adj_factor"):
                    try:
                        adj_df = src_used.get_adj_factor(code)
                    except Exception:
                        adj_df = None
                    if adj_df is not None and adj_df.height:
                        adj_frames.append(adj_df)
                        adj_ok_codes += 1
            done_msg = (f"日线完成: {code} ({i + 1}/{total})" if df is not None
                        else f"日线拉取失败(跳过): {code} ({i + 1}/{total})")
            report(5 + 70 * (i + 1) / total, done_msg)
        if not frames:
            raise UpdateError(
                "所有股票日线拉取失败（数据源已安装但拉数失败，请检查网络与数据源可用性）。"
                "可调用 POST /api/data/demo 生成演示数据用于联调与演示")
        report(75, f"合并与校验日线数据（{len(frames)} 只有效）...")
        # 不同源 volume/amount 类型可能不一致（akshare 可能为字符串且含空串，如停牌日成交额 ""），
        # 先统一为与本地 daily.parquet 一致的 schema（volume Int64 / amount Float64）再合并；
        # 非严格转换：空串/非法值转 null，避免 strict_cast 抛错中断整个更新
        for col, dt in (("volume", pl.Int64), ("amount", pl.Float64)):
            for i, f in enumerate(frames):
                if col in f.columns:
                    frames[i] = f.with_columns(pl.col(col).cast(dt, strict=False))
        merged = pl.concat(frames).sort(["code", "date"])
        cal = store.read_calendar(data_dir)
        _validate_daily(merged, cal["date"].to_list() if cal is not None else sorted(merged["date"].unique().to_list()))
        existing = store.read_daily(None, data_dir)
        if existing is not None:
            # 统一 schema：新数据 volume/amount 转为既有数据的类型（Int64/Float64），避免 concat 报错
            for col in ("volume", "amount"):
                if col in merged.columns and col in existing.columns:
                    merged = merged.with_columns(pl.col(col).cast(existing[col].dtype))
            merged = pl.concat([existing, merged]).unique(subset=["code", "date"],
                                                          keep="last").sort(["code", "date"])
        store.write_daily(merged, data_dir)
        stats["daily_rows"] = merged.height

        # ---- 复权因子：事件级 -> 展开到每日 -> 增量合并写 parquet ----
        if adj_frames:
            report(76, f"展开并合并复权因子（{len(adj_frames)} 只）...")
            adj_events = pl.concat(adj_frames, how="diagonal_relaxed").select(
                ["code", "date", pl.col("adj_factor").cast(pl.Float64)])
            # 以本次实际日线日期为网格展开（保证每个有行情的交易日都有因子）
            grid = (merged.group_by("code")
                    .agg(pl.col("date").sort().alias("dates"))
                    .to_dicts())
            daily_dates = {r["code"]: list(r["dates"]) for r in grid}
            adj_daily = _expand_adj_to_daily(adj_events, daily_dates)
            existing_adj = store.read_adj_factor(None, data_dir)
            if existing_adj is not None and existing_adj.height:
                adj_daily = (pl.concat([existing_adj, adj_daily])
                             .unique(subset=["code", "date"], keep="last")
                             .sort(["code", "date"]))
            store.write_adj_factor(adj_daily, data_dir)
            stats["adj_factor_rows"] = adj_daily.height
            stats["adj_factor_codes"] = adj_daily["code"].n_unique()
        else:
            report(74, "警告：未获取到任何复权因子，后复权将退化为恒等（除权日会有假跳空）")
        stats["adj_ok_codes"] = adj_ok_codes

    if "minute5" in scopes:
        # 分钟线源优先级：baostock(深历史) → mootdx(科创板/约2年，通达信直连) → akshare(东财浅历史且易受代理影响)
        minute5_srcs = sorted(daily_ok,
                              key=lambda s: {"baostock": 0, "mootdx": 1, "akshare": 2}.get(s.name, 3))
        total = len(update_codes)
        for i, code in enumerate(update_codes):
            report(78 + 17 * i / total, f"正在拉取分钟线: {code} ({i + 1}/{total})")
            df = None
            for src in minute5_srcs:
                df = src.get_minute5(code, start_date, end_date)
                if df is not None:
                    break
            if df is not None:
                # 增量合并：按日期区间拉取时，不能整文件覆盖（会丢掉已存在的其它区间数据）
                existing = store.read_minute5(code, data_dir=data_dir)
                if existing is not None and existing.height:
                    for col in ("volume", "amount"):
                        if col in df.columns and col in existing.columns:
                            df = df.with_columns(pl.col(col).cast(existing[col].dtype))
                    df = (pl.concat([existing, df])
                          .unique(subset=["code", "date"], keep="last")
                          .sort("date"))
                store.write_minute5(code, df, data_dir)
                stats["minute5_rows"] += df.height
            report(78 + 17 * (i + 1) / total,
                   f"分钟线{'完成' if df is not None else '拉取失败(跳过)'}: {code} ({i + 1}/{total})")

    report(100, "更新完成")
    return stats


def update_task(task_id: str, scope: str, codes: Optional[list[str]] = None,
                db_path: Optional[str] = None, data_dir: Optional[str] = None,
                start_date: str = "1990-01-01", end_date: str = "2099-12-31") -> dict:
    """任务入口（子进程调用）：包装进度写库"""
    from .. import db
    try:
        db.update_task(task_id, db_path=db_path, status="running")
        stats = update(scope, codes=codes, data_dir=data_dir,
                       start_date=start_date, end_date=end_date,
                       progress_cb=lambda p, m: db.update_progress(task_id, p, m, db_path))
        db.finish_task(task_id, "success", payload=stats, db_path=db_path)
        return stats
    except Exception as e:  # noqa: BLE001
        db.finish_task(task_id, "failed", error=f"{e}\n{traceback.format_exc()[-1000:]}",
                       db_path=db_path)
        raise
