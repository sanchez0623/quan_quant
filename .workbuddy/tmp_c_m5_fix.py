# -*- coding: utf-8 -*-
"""分钟缺口补拉（2022口径）：4,238 码，按 c_m5_fix_plan.json 逐段补。
- baostock 单连接串行（铁律）；黑名单启动检查
- 断点续传：cseg_m5_done.txt 仅记成功码 + 启动自校验剔除实际仍有缺口者
- 合并 keep="first"（库内已有 bar 零覆盖）
- 逐票原子写盘，被杀只丢当前票
"""
import sys, json, time, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store, bs_usage
from app.data.sources import BaostockSource

WORK = ROOT / ".workbuddy"
DATA = str(ROOT / "data")
PLAN_F = WORK / "c_m5_fix_plan.json"
DONE_F = WORK / "cseg_m5_done.txt"

plan = json.loads(PLAN_F.read_text(encoding="utf-8"))
done = set(DONE_F.read_text(encoding="utf-8").split()) if DONE_F.exists() else set()

# 启动自校验：done 中仍有缺口的码剔除重拉
if done:
    dsub = store.read_daily(sorted(done), DATA)
    day_chk = {r["code"]: set(r["ds"]) for r in
               (dsub.filter(pl.col("volume") > 0)
                .group_by("code").agg(pl.col("date").unique().alias("ds")).to_dicts())}
    still = []
    for c in list(done):
        if c not in plan:
            continue
        have = set()
        p = ROOT / "data" / "minute5" / f"{c}.parquet"
        if p.exists():
            try:
                m5 = pl.read_parquet(p, columns=["date"])
                have = set(m5["date"].str.slice(0, 10).unique().to_list())
            except Exception:
                have = set()
        if set(plan[c][0:1] and [plan[c][0][0]]) and False:
            pass
        need = set()
        for s, e in plan[c]:
            import datetime
            d0, d1 = datetime.date.fromisoformat(s), datetime.date.fromisoformat(e)
            # 用日线日期精确判断（day_chk 有该码全部 vol>0 日期）
            need |= {d for d in day_chk.get(c, set()) if s <= d <= e}
        if need - have:
            still.append(c)
    if still:
        print(f"自校验: {len(still)} 个已记 done 的码仍有缺口, 剔除重拉: {still[:10]}", flush=True)
        done -= set(still)

todo = [c for c in sorted(plan) if c not in done]
n_seg = sum(len(plan[c]) for c in todo)
print(f"待拉 {len(todo)} 码 / {n_seg} 段", flush=True)
if bs_usage.tracker.is_blacklisted():
    info = bs_usage.tracker.last_blacklist() or {}
    print(f"[阻断] baostock 黑名单中，预计 {info.get('release_at')} 解除", flush=True)
    sys.exit(0)

bs = BaostockSource()
state = {"ok": 0, "fail": 0, "empty_streak": 0}
t0 = time.time()

def reconnect():
    try:
        bs._force_logout()
    except Exception:
        pass
    time.sleep(3)
    try:
        bs._ensure_login()
    except Exception:
        pass

def fetch(c, s, e):
    for attempt in range(3):
        try:
            return bs.get_minute5(c, s, e)
        except Exception as ex:  # noqa: BLE001
            real = "BsBlacklisted" in type(ex).__name__ and bs_usage.tracker.is_blacklisted()
            print(f"  {c} {s}~{e} 异常{attempt + 1}({'真黑名单' if real else '抖动'}): {type(ex).__name__}", flush=True)
            if real:
                return None
            time.sleep(2)
            reconnect()
    return None

with DONE_F.open("a", encoding="utf-8") as fd:
    for i, c in enumerate(todo):
        existing = store.read_minute5(c, data_dir=DATA)
        new_frames = []
        seg_fail = 0
        for s, e in plan[c]:
            df = fetch(c, s, e)
            if df is not None and df.height:
                new_frames.append(df)
                state["empty_streak"] = 0
            else:
                seg_fail += 1
                state["empty_streak"] += 1
                if state["empty_streak"] >= 10:
                    print("  连续 10 段空，强制重连", flush=True)
                    reconnect()
                    state["empty_streak"] = 0
        if new_frames:
            new = pl.concat(new_frames).unique(subset=["date"], keep="last").sort("date")
            if existing is not None and existing.height:
                for col in ("volume", "amount"):
                    if col in new.columns and col in existing.columns:
                        new = new.with_columns(pl.col(col).cast(existing[col].dtype, strict=False))
                merged = pl.concat([existing, new]).unique(subset=["date"], keep="first").sort("date")
            else:
                merged = new
            store.write_minute5(c, merged, data_dir=DATA)
            state["ok"] += 1
        if seg_fail == 0:
            fd.write(c + "\n")
            fd.flush()
        if (i + 1) % 50 == 0 or i + 1 == len(todo):
            rate = (i + 1) / max(time.time() - t0, 1)
            remain = (len(todo) - i - 1) / max(rate, 0.01)
            print(f"[{i + 1}/{len(todo)}] {c} 成功 {state['ok']} 失败 {state['fail'] + seg_fail} "
                  f"速率 {rate:.2f}码/s 预计剩 {remain / 3600:.1f}h", flush=True)

print(f"\n=== 本轮完成: 成功 {state['ok']} 码 ===", flush=True)
