# -*- coding: utf-8 -*-
"""分钟线中段缺口定向补洞（看门狗版 HOLE_FILL）。

背景（2026-09-20 复核）：全市场 minute5 存在 783 只票的"日中洞"
（日线有 bar 而 minute5 缺该日，排除停牌），合计 ~7 万缺口日。主体为
科创板 2024-01-02→08-15 连续块——初始数据源对 688 深度截断 + B 步
mootdx 兜底仅 ~2 年深度；A/B 断点只查文件尾部、查不到中段洞。
已实测 baostock 可完整取到 688 的 2024H1。

机制：
- 扫描：真缺口 = 日线有 bar & minute 缺日（停牌合法缺失自动排除），
  按票压缩成连续区段（间隔 <=3 交易日合并）
- 定向拉取：每票只拉缺口区段，baostock 优先 -> mootdx 兜底，
  区段帧合并回原文件（unique(code,date) keep=last，幂等）
- 看门狗：与 _backfill_minute5 相同（停滞 8min 杀树 / worker 15min
  轮换 / 同票 2 次停滞进跳过名单），断点 = 剩余真缺口文件态重算
- 跳过名单重试：无 minute5 文件的在市票给整窗口机会

用法：python scripts/_backfill_m5_holes.py（建议 Start-Process 脱离终端）
"""
import json
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app import config, db
from app.data import store

OUT = BACKEND / "scripts" / "out"
WORKER = BACKEND / "scripts" / "_backfill_m5_holes_worker.py"
WIN_START = "2024-01-02"     # 策略窗口起点：更早的洞不补
STALL_SEC = 480
RECYCLE_SEC = 900
POLL_SEC = 15
MAX_STRIKES = 2
MERGE_GAP_DAYS = 3           # 区段间隔 <=3 交易日合并，减少请求数

_BEFORE_RE = re.compile(r"补洞:\s*(\d{6})\s*\((\d+)/")
_AFTER_RE = re.compile(r"补洞(?:完成|失败\(跳过\)):\s*(\d{6})\s*\((\d+)/")


def _scan_jobs() -> tuple[list[dict], dict[str, set]]:
    """真缺口扫描 -> 按票区段任务；返回 (jobs, 每票应有交易日集)"""
    basic = store.read_stock_basic()
    inmarket = set(basic.filter(~pl.col("delisted"))["code"].to_list())
    daily = store.read_daily()
    daily_pairs = (daily.filter((pl.col("date") >= WIN_START)
                                & (pl.col("date") <= "2099-12-31")
                                & (pl.col("volume") > 0))
                   .select(["code", "date"]).unique())
    need: dict[str, set] = {}
    for c, d in daily_pairs.filter(pl.col("code").is_in(sorted(inmarket))).rows():
        need.setdefault(c, set()).add(d)

    files = sorted(config.MINUTE5_DIR.glob("*.parquet"))

    def minute_days(p: Path):
        try:
            df = pl.read_parquet(p, columns=["date"])
            return p.stem, {x[:10] for x in df["date"].to_list()
                            if x[:10] >= WIN_START}
        except Exception:
            return p.stem, set()

    with ThreadPoolExecutor(8) as ex:
        have = dict(ex.map(minute_days, files))
    have.update({c: set() for c in inmarket - set(have)})

    cal = store.read_calendar()
    cal_days = sorted(cal.filter((pl.col("is_open").cast(pl.Int8) == 1)
                                 & (pl.col("date") >= WIN_START))["date"].to_list())
    jobs: list[dict] = []
    for c in sorted(inmarket):
        holes = sorted(need.get(c, set()) - have.get(c, set()))
        if not holes:
            continue
        segs: list[list[str]] = []
        for d in holes:
            if segs and d <= segs[-1][1]:
                segs[-1][1] = d
            else:
                segs.append([d, d])
        merged = []
        for s, e in segs:
            i0 = cal_days.index(s)
            if merged:
                prev_e = merged[-1][1]
                gap = cal_days.index(s) - cal_days.index(prev_e)
                if gap <= MERGE_GAP_DAYS + 1:
                    merged[-1][1] = e
                    continue
            merged.append([s, e])
        jobs.append({"code": c, "segments": merged})
    return jobs, need


def _spawn_worker(jobs: list, state: Path):
    jobs_file = OUT / "_m5_holes_jobs.json"
    jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    venv_py = BACKEND.parent / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    return subprocess.Popen(
        [py, str(WORKER), str(jobs_file), str(state)],
        cwd=str(BACKEND), stdout=(OUT / "_m5_holes_worker.log").open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT)


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _read_state(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_watchdog(jobs: list[dict], need: dict[str, set], task_name: str) -> dict:
    """与 _backfill_minute5 同构的看门狗；断点 = 剩余真缺口重算"""
    total = len(jobs)
    if total == 0:
        print("[补洞] 无缺口", flush=True)
        return {"done": 0, "total": 0}
    tid = "data_" + uuid.uuid4().hex[:12]
    db.create_task(tid, task_name, "data_update",
                   payload={"scope": "minute5_holes", "jobs": total,
                            "watchdog": True})
    db.update_task(tid, status="running")
    print(f"[{task_name}] task={tid} 目标 {total} 票", flush=True)

    skip_path = OUT / "_m5_holes_skip.json"
    skips: set = set(json.loads(skip_path.read_text(encoding="utf-8"))
                     if skip_path.exists() else [])
    strikes: dict[str, int] = {}
    passes = fails = 0
    done_at_fail = -1
    n_stall = n_recycle = 0
    state = OUT / "_m5_holes_state.json"

    def _holes_all() -> list:
        """全部仍有真缺口的票（含跳过名单内，仅作统计口径）"""
        return [j for j in jobs
                if need.get(j["code"], set()) - _have_days(j["code"])]

    while True:
        raw = _holes_all()
        remaining = [j for j in raw if j["code"] not in skips]
        if not remaining:
            break
        done_base = total - len(raw)
        state.unlink(missing_ok=True)
        proc = _spawn_worker(remaining, state)
        born = time.time()
        last_key = None
        last_move = time.time()
        rc = None
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            st = _read_state(state)
            key = (st or {}).get("m")
            if key != last_key:
                last_key, last_move = key, time.time()
            if time.time() - last_move > STALL_SEC:
                hung = _BEFORE_RE.search(str((st or {}).get("m") or ""))
                _kill_tree(proc.pid)
                n_stall += 1
                if hung:
                    c = hung.group(1)
                    strikes[c] = strikes.get(c, 0) + 1
                    print(f"[补洞] 停滞杀死（疑挂 {c}，第 {strikes[c]} 次）",
                          flush=True)
                    if strikes[c] >= MAX_STRIKES:
                        skips.add(c)
                        skip_path.write_text(json.dumps(sorted(skips)),
                                             encoding="utf-8")
                        print(f"[补洞] {c} 两次停滞 -> 跳过名单", flush=True)
                else:
                    print("[补洞] 停滞杀死（未定位）", flush=True)
                break
            if time.time() - born > RECYCLE_SEC:
                _kill_tree(proc.pid)
                n_recycle += 1
                break
            m = _AFTER_RE.search(str((st or {}).get("m") or ""))
            cur = done_base + (min(len(remaining), int(m.group(2))) if m else 0)
            try:
                db.update_progress(tid, round(cur / max(total, 1) * 100),
                                   f"补洞 ({cur}/{total})")
            except db.TaskCancelled:
                _kill_tree(proc.pid)
                db.finish_task(tid, "cancelled", error="已被用户取消")
                return {"cancelled": True}
            time.sleep(POLL_SEC)
        if rc is None:
            continue   # 停滞/轮换 -> 文件态重算断点续跑
        if rc != 0:
            fails += 1
            done_now = total - len(_holes_all())
            if fails >= 3 and done_now == done_at_fail:
                db.finish_task(tid, "failed",
                               error=f"worker 连续 {fails} 次异常退出且无进展")
                return {"failed": True}
            done_at_fail = done_now
            continue
        passes += 1
        left = [j for j in _holes_all() if j["code"] not in skips]
        if not left:
            break
        if passes >= 2:
            skips.update(j["code"] for j in left)
            skip_path.write_text(json.dumps(sorted(skips)), encoding="utf-8")
            print(f"[补洞] {len(left)} 票两轮仍有洞 -> 跳过名单收尾", flush=True)
            break
        print(f"[补洞] 本轮结束仍剩 {len(left)} 票，重试一轮", flush=True)

    done = total - len(_holes_all())
    payload = {"done": done, "total": total, "skipped": sorted(skips),
               "stall_kills": n_stall, "recycles": n_recycle}
    try:
        db.update_progress(tid, 100, f"补洞 完成({done}/{total})")
    except db.TaskCancelled:
        pass
    db.finish_task(tid, "success", payload=payload)
    print(f"[补洞] 完成 {done}/{total}，跳过 {len(skips)}，停滞 {n_stall}，"
          f"轮换 {n_recycle}", flush=True)
    return payload


def _have_days(code: str) -> set:
    p = config.MINUTE5_DIR / f"{code}.parquet"
    if not p.exists():
        return set()
    try:
        df = pl.read_parquet(p, columns=["date"])
        return {x[:10] for x in df["date"].to_list() if x[:10] >= WIN_START}
    except Exception:
        return set()


def main() -> None:
    # 开工前检查 baostock 黑名单（限制期内直接退出，子进程启动后才发现太晚）
    from app.data.bs_usage import tracker as _bs_tracker
    if _bs_tracker.is_blacklisted():
        info = _bs_tracker.last_blacklist() or {}
        release = info.get("release_at") or "稍后"
        print(f"[阻断] baostock IP 黑名单限制中，预计 {release} 解除，退出", flush=True)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    jobs, need = _scan_jobs()
    hole_days = sum(len(need.get(j["code"], set()) - _have_days(j["code"]))
                    for j in jobs)
    print(f"[扫描] 缺口票 {len(jobs)} 只，缺口日 {hole_days} 天，"
          f"耗时 {time.time() - t0:.0f}s", flush=True)
    _run_watchdog(jobs, need, "分钟线补洞：中段缺口定向回补")
    print("补洞全部完成", flush=True)


if __name__ == "__main__":
    main()
