# -*- coding: utf-8 -*-
"""B1：全库日K缺行补拉（按段，baostock 单连接串行）。
段在内存攒齐后一次性与全表合并单次写库（铁律：不循环写库）。"""
import sys, time, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app import config  # noqa: F401
from app.data import store
from app.data.sources import BaostockSource

START = "2021-01-01"
DATA = str(ROOT / "data")
daily_all = store.read_daily(None, DATA)
END = str(daily_all["date"].max())
cal = store.read_calendar(DATA)
cal_dates = [d for d in cal["date"].to_list() if START <= d <= END]
cal_index = {d: i for i, d in enumerate(cal_dates)}

have_map = {r["code"]: set(r["ds"]) for r in
            (daily_all.filter((pl.col("date") >= START) & (pl.col("date") <= END))
             .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}

# 重算缺行段
plan = {}
for c, have in have_map.items():
    f, l = min(have), max(have)
    miss = [d for d in cal_dates if f <= d <= l and d not in have]
    if not miss:
        continue
    runs, s, p, pi = [], miss[0], miss[0], cal_index[miss[0]]
    for d in miss[1:]:
        if cal_index[d] - pi <= 3:
            p = d
            pi = cal_index[d]
        else:
            runs.append((s, p))
            s, p, pi = d, d, cal_index[d]
    runs.append((s, p))
    plan[c] = runs

total_days = sum(sum(cal_index[e] - cal_index[s] + 1 for s, e in runs) for runs in plan.values())
print(f"缺行 {len(plan)} 码 / {len(plan)} 码共 {sum(len(r) for r in plan.values())} 段 / 约 {total_days} 天, END={END}", flush=True)

bs = BaostockSource()
frames, ok, fail = [], 0, []
t0 = time.time()
for i, (c, runs) in enumerate(sorted(plan.items())):
    got = 0
    for s, e in runs:
        df = None
        for attempt in range(2):
            try:
                df = bs.get_daily(c, s, e)
                break
            except Exception as ex:  # noqa: BLE001
                print(f"  {c} {s}~{e} 异常{attempt + 1}: {type(ex).__name__}", flush=True)
                time.sleep(2)
                try:
                    bs._force_logout(); bs._ensure_login()
                except Exception:
                    pass
        if df is not None and df.height:
            frames.append(df)
            got += df.height
        else:
            fail.append((c, s, e))
    ok += 1
    if (i + 1) % 100 == 0:
        rate = (i + 1) / max(time.time() - t0, 1)
        print(f"[{i + 1}/{len(plan)}] 码 {c} 累计行 {sum(f.height for f in frames)} "
              f"速率 {rate:.1f}码/s 预计剩 {(len(plan) - i - 1) / max(rate, 0.01) / 60:.0f} 分钟", flush=True)

print(f"\n拉取完成: {ok} 码, {sum(f.height for f in frames)} 行, 失败段 {len(fail)}: {fail[:10]}", flush=True)

if frames:
    new = pl.concat(frames).select(["code", "date", "open", "high", "low", "close",
                                    pl.col("volume").cast(pl.Int64, strict=False),
                                    pl.col("amount").cast(pl.Float64, strict=False)])
    existing = store.read_daily(None, DATA)
    for col, dt in (("volume", pl.Int64), ("amount", pl.Float64)):
        new = new.with_columns(pl.col(col).cast(dt, strict=False))
        if col in existing.columns:
            existing = existing.with_columns(pl.col(col).cast(existing[col].dtype))
    merged = (pl.concat([existing, new]).unique(subset=["code", "date"], keep="last")
              .sort(["code", "date"]))
    store.write_daily(merged, DATA)
    print(f"合并写库: {merged.height} 行 / {merged['code'].n_unique()} 码", flush=True)

# 复验
daily2 = store.read_daily(None, DATA)
have2 = {r["code"]: set(r["ds"]) for r in
         (daily2.filter((pl.col("date") >= START) & (pl.col("date") <= END))
          .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
left_codes, left_days = 0, 0
for c in plan:
    have = have2.get(c, set())
    f, l = min(have), max(have) if have else (None, None)
    if not have:
        left_codes += 1
        continue
    miss = [d for d in cal_dates if f <= d <= l and d not in have]
    if miss:
        left_codes += 1
        left_days += len(miss)
print(f"=== B1 DONE: 剩余缺行 {left_codes} 码 / {left_days} 天 ===", flush=True)
