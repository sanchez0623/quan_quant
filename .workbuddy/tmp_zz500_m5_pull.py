# -*- coding: utf-8 -*-
"""串行补拉 zz500 历史成分码 5分钟线缺口（2021-01-01~2026-09-07）。
- 只拉缺口段（探查产物 zz500_m5_need.json），不碰已有区间；
- 合并 keep="first"（库内已有行保留，满足"已有的不覆盖"）；
- baostock 单连接串行；连续 10 段空强制重连；断点续传（zz500_m5_done.txt）。
"""
import sys, json, time, pathlib
import polars as pl
ROOT = pathlib.Path(r"d:\Sanchez\AI\TraeProjects\quan_quant")
sys.path.insert(0, str(ROOT / "backend"))
from app.data import store
from app.data import bs_usage
from app.data.sources import BaostockSource

WORK = ROOT / ".workbuddy"
DATA = str(ROOT / "data")
plan = json.loads((WORK / "zz500_m5_need.json").read_text(encoding="utf-8"))
DONE_F = WORK / "zz500_m5_done.txt"
FAIL_F = WORK / "zz500_m5_failed.jsonl"

done = set(DONE_F.read_text(encoding="utf-8").split()) if DONE_F.exists() else set()
plan = json.loads((WORK / "zz500_m5_need.json").read_text(encoding="utf-8"))
# 凡当前仍有缺口的码一律不算完成（防"拉取不完整却被标 done"导致永久漏补）
done -= set(plan)
codes = [c for c in sorted(plan) if c not in done]
n_segs = sum(len(plan[c]) for c in codes)
print(f"待拉 {len(codes)} 码 / {n_segs} 段", flush=True)

bs = BaostockSource()
state = {"empty": 0, "ok_codes": 0, "got_segs": 0, "fail_segs": 0}

def reconnect():
    try:
        bs._force_logout()
    except Exception:
        pass
    time.sleep(3)
    bs._ensure_login()

def fetch_seg(c, s, e):
    for attempt in range(3):
        try:
            return bs.get_minute5(c, s, e)
        except Exception as ex:  # noqa: BLE001
            tag = ("真黑名单" if "BsBlacklisted" in type(ex).__name__
                   and bs_usage.tracker.is_blacklisted() else "网络抖动/误报")
            print(f"  {c} 段 {s}~{e} 第{attempt + 1}次异常({tag}): {type(ex).__name__}", flush=True)
            time.sleep(2)
            try:
                reconnect()
            except Exception:
                pass
    return None

t0 = time.time()
with FAIL_F.open("a", encoding="utf-8") as ff, DONE_F.open("a", encoding="utf-8") as fd:
    for i, c in enumerate(codes):
        existing = store.read_minute5(c, data_dir=DATA)
        new_frames = []
        seg_fail_local = 0
        for s, e in plan[c]:
            df = fetch_seg(c, s, e)
            if df is not None and df.height:
                new_frames.append(df)
                state["got_segs"] += 1
                state["empty"] = 0
            else:
                state["empty"] += 1
                state["fail_segs"] += 1
                seg_fail_local += 1
                ff.write(json.dumps({"code": c, "start": s, "end": e}) + "\n")
                ff.flush()
                if state["empty"] >= 10:
                    print("  连续 10 段空，强制重连", flush=True)
                    reconnect()
                    state["empty"] = 0
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
            state["ok_codes"] += 1
        if seg_fail_local == 0:
            fd.write(c + "\n")
            fd.flush()
        if (i + 1) % 20 == 0 or i + 1 == len(codes):
            rate = (i + 1) / max(time.time() - t0, 1)
            remain = (len(codes) - i - 1) / max(rate, 0.01)
            print(f"[{i + 1}/{len(codes)}] {c} 成功码 {state['ok_codes']} "
                  f"段 {state['got_segs']}/{n_segs} 失败段 {state['fail_segs'] + 0} "
                  f"速率 {rate:.2f}码/s 预计剩 {remain / 60:.0f} 分钟", flush=True)

print("\n=== DONE ===", flush=True)
print(f"成功码 {state['ok_codes']}，获得段 {state['got_segs']}", flush=True)
