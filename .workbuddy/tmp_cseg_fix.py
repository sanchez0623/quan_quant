# -*- coding: utf-8 -*-
"""C1：全库 2022 口径缺段清单生成 + C2 断点续传补拉脚本
- 缺段码 = 日线首日 > 2022-01-04 且 (无上市日 或 首日-上市日>14天)
- 补拉区间 = 2022-01-04 ~ min(首日前一交易日, 最新)
- 串行 baostock 单连接；done 断点续传；内存攒批一次性合并写库（铁律）
- 因子：对每个新补码重拉全历史事件，flush 时展开到该码全部日线日期后一次性合并
"""
import sys, time, json, pathlib, datetime
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource
from app.data.updater import _expand_adj_to_daily

WORK = ROOT / ".workbuddy"
DATA = str(ROOT / "data")
FIRST_TD = "2022-01-04"
DONE_F = WORK / "cseg_done.txt"

daily_all = store.read_daily(None, DATA)
END = str(daily_all["date"].max())
cal = store.read_calendar(DATA)
cal_dates = [d for d in cal["date"].to_list() if FIRST_TD <= d <= END]
first_map = {r["code"]: r["first"] for r in
             (daily_all.group_by("code").agg(pl.col("date").min().alias("first"))
              .to_dicts())}
have_map = {r["code"]: sorted(set(r["ds"])) for r in
            (daily_all.filter((pl.col("date") >= FIRST_TD))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}

# 上市日：直接查 baostock（可靠，不依赖缓存 json）
bs0 = BaostockSource()
def _q_basic():
    rs = bs0._bs.query_stock_basic()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rs, (rs.fields, rows)
_res = bs0._run_query(_q_basic)
ipo_map = {}
if _res:
    fields, rows = _res
    i_code, i_ipo = fields.index("code"), fields.index("ipoDate")
    for r in rows:
        ipo_map[r[i_code].split(".")[-1]] = r[i_ipo]
print(f"baostock 基础信息: {len(ipo_map)} 只", flush=True)

plan = {}
for c, f in first_map.items():
    if f <= FIRST_TD:
        continue
    ipo = ipo_map.get(c)
    if ipo and ipo >= FIRST_TD:
        continue   # 2022-01-04 后上市的次新股：2022 前无数据，非缺口
    if ipo:
        try:
            lag = (datetime.date.fromisoformat(f) - datetime.date.fromisoformat(ipo)).days
            if lag <= 14:
                continue
        except Exception:
            pass
    # 补 2022-01-04 ~ 首日前一交易日
    idx = cal_dates.index(f) if f in cal_dates else None
    if idx is None:
        continue
    end = cal_dates[idx - 1] if idx > 0 else None
    if end is None or end < FIRST_TD:
        continue
    plan[c] = [FIRST_TD, end]

done = set(DONE_F.read_text(encoding="utf-8").split()) if DONE_F.exists() else set()

# 自校验：done 中的码若实际仍有缺口（曾失败被误记 done），剔除重拉
if done:
    dsub = store.read_daily(list(done), DATA)
    have_chk = {r["code"]: set(r["ds"]) for r in
                (dsub.filter((pl.col("date") >= FIRST_TD))
                 .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
    still_gap = []
    for c in list(done):
        if c not in plan:
            continue   # 已补完（首日已被拉到 2022-01-04），不再需要
        have = have_chk.get(c, set())
        s, e = plan[c]
        need = {d for d in cal_dates if s <= d <= e}
        if need - have:
            still_gap.append(c)
    if still_gap:
        print(f"自校验: {len(still_gap)} 个已记 done 的码实际仍有缺口, 剔除重拉: {still_gap[:10]}", flush=True)
        done -= set(still_gap)

todo = [c for c in sorted(plan) if c not in done]
n_days = sum(sum(1 for d in cal_dates if s <= d <= e) for s, e in plan.values())
print(f"缺段码 {len(plan)}（2022 口径）| 待拉 {len(todo)} | 累计 {n_days} 交易日, END={END}", flush=True)

bs = BaostockSource()
frames, ev_frames = [], []
ok, fail = 0, []
t0 = time.time()
BATCH = 150

def flush():
    global frames, ev_frames
    if frames:
        new = pl.concat(frames).select(
            ["code", "date", "open", "high", "low", "close",
             pl.col("volume").cast(pl.Int64, strict=False),
             pl.col("amount").cast(pl.Float64, strict=False)])
        existing = store.read_daily(None, DATA)
        for col, dt in (("volume", pl.Int64), ("amount", pl.Float64)):
            if col in existing.columns:
                new = new.with_columns(pl.col(col).cast(existing[col].dtype))
        merged = (pl.concat([existing, new]).unique(subset=["code", "date"], keep="last")
                  .sort(["code", "date"]))
        store.write_daily(merged, DATA)
        print(f"  日线合并写库: {merged.height} 行", flush=True)
        frames = []
    if ev_frames:
        ev = pl.concat(ev_frames, how="diagonal_relaxed").select(
            ["code", "date", pl.col("adj_factor").cast(pl.Float64)])
        grid = {r["code"]: list(r["ds"]) for r in
                (store.read_daily(None, DATA).filter(pl.col("date") >= FIRST_TD)
                 .group_by("code").agg(pl.col("date").unique().sort().alias("ds")).to_dicts())}
        daily_fac = _expand_adj_to_daily(ev, {c: grid.get(c, []) for c in ev["code"].unique().to_list()})
        existing_a = store.read_adj_factor(None, DATA)
        merged_a = (pl.concat([existing_a, daily_fac]).unique(subset=["code", "date"], keep="last")
                    .sort(["code", "date"]))
        store.write_adj_factor(merged_a, DATA)
        print(f"  因子合并写库: {merged_a.height} 行", flush=True)
        ev_frames = []

with DONE_F.open("a", encoding="utf-8") as fd:
    for i, c in enumerate(todo):
        s, e = plan[c]
        df = None
        for attempt in range(3):
            try:
                df = bs.get_daily(c, s, e)
                break
            except Exception as ex:  # noqa: BLE001
                print(f"  {c} 日线异常{attempt + 1}: {type(ex).__name__}", flush=True)
                time.sleep(2)
                try:
                    bs._force_logout(); bs._ensure_login()
                except Exception:
                    pass
        if df is not None and df.height:
            frames.append(df)
            ok += 1
            # 因子事件（全历史）
            for attempt in range(2):
                try:
                    ev = bs.get_adj_factor(c, start="1990-01-01")
                    break
                except Exception:
                    ev = None
                    try:
                        bs._force_logout(); bs._ensure_login()
                    except Exception:
                        pass
            if ev is not None and ev.height:
                ev_frames.append(ev)
        else:
            fail.append((c, s, e))
        if df is not None and df.height:
            fd.write(c + "\n")   # 仅成功码记 done
            fd.flush()
        if len(frames) >= BATCH:
            flush()
        if (i + 1) % 200 == 0 or i + 1 == len(todo):
            rate = (i + 1) / max(time.time() - t0, 1)
            remain = (len(todo) - i - 1) / max(rate, 0.01)
            print(f"[{i + 1}/{len(todo)}] {c} ok={ok} fail={len(fail)} "
                  f"速率 {rate:.2f}码/s 预计剩 {remain / 3600:.1f}h", flush=True)
flush()

print(f"\n=== C2 本段完成: 成功 {ok} 码, 失败 {len(fail)}: {fail[:10]} ===", flush=True)
print(f"剩余待拉: {len(plan) - len(done) - ok - len(fail)} 码（下次续跑自动继续）", flush=True)
