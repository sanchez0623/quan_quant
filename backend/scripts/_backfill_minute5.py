# -*- coding: utf-8 -*-
"""5分钟线一次性缺口回补（看门狗版 WATCHDOG_BACKFILL）。

背景（2026-09-17 21:14 事故）：B 步全窗口拉取时，个别票 baostock 失败降级
mootdx（TDX 7709），服务器掐断连接（TCP CLOSE_WAIT）后客户端库死循环
（CPU 98%），任务永久挂在 running。本版由父进程驱动 worker 子进程：

- 断点续传（B）：missing = 无 minute5 文件的票，重启自动跳过已完成
- 停滞看门狗：state/文件 8 分钟无变化 -> 杀树重启（taskkill /F /T）
- 生命周期轮换：worker 15 分钟强制回收（连接活不过 ~13 分钟的观察值）
- 挂起计次：同一票 2 次停滞 -> 进跳过名单（scripts/out/_backfill_skip.json）
- 两轮兜底：worker 正常跑完仍无文件的票（源返回空），重试 1 轮后跳过
- A 步新鲜度守卫：对齐日已达今日 -> 跳过（避免重跑 2923 次空请求）
- 数据完整性：收尾扫描新文件日期覆盖，疑似截断（起点晚于 2023-04）计数上报

用法：python scripts/_backfill_minute5.py（建议经 Start-Process 脱离终端启动）
"""
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app import config, db
from app.data import store

BACKFILL_START_B = "2024-01-01"  # 2026-09-17 用户拍板缩窗口：主验证 2024-2025 IS + 2026 OOS；2023 跨窗段退化为日线级验证
STALL_SEC = 480        # 停滞判定：state/文件 8 分钟无变化（正常节奏 ~130s/票）
RECYCLE_SEC = 900      # worker 生命周期上限 15 分钟（观察值：mootdx 连接 ~13 分钟被掐）
POLL_SEC = 15
MAX_STRIKES = 2        # 同一票停滞达 2 次 -> 跳过名单
MAX_FAIL_PASSES = 2    # 整轮正常跑完仍无文件的票：最多 2 轮后跳过
OUT = BACKEND / "scripts" / "out"
WORKER = BACKEND / "scripts" / "_backfill_minute5_worker.py"

_BEFORE_RE = re.compile(r"正在拉取分钟线:\s*(\d{6})\s*\((\d+)/")
_AFTER_RE = re.compile(r"分钟线(?:完成|拉取失败\(跳过\)):\s*(\d{6})\s*\((\d+)/")


def minute5_codes() -> set:
    return {p.stem for p in config.MINUTE5_DIR.glob("*.parquet")}


def aligned_last_day() -> str:
    """全库分钟线最后交易日的众数（YYYY-MM-DD）"""
    files = sorted(config.MINUTE5_DIR.glob("*.parquet"))

    def _max(fp: Path):
        try:
            return pl.scan_parquet(fp).select(pl.col("date").max()).collect().item()
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        vals = [v for v in ex.map(_max, files) if v]
    if not vals:
        raise RuntimeError("minute5 目录为空，无法确定对齐日")
    return Counter(v[:10] for v in vals).most_common(1)[0][0]


def _all_codes() -> list[str]:
    basic = store.read_stock_basic()
    if basic is None:
        raise RuntimeError("stock_basic 不存在，无法确定更新范围")
    return (basic.filter(~pl.col("delisted"))["code"].to_list()
            if "delisted" in basic.columns else basic["code"].to_list())


def _cancel_stuck_legacy() -> None:
    """启动时收编遗留任务（幂等）：取消任何 running/pending 态的旧回补任务
    （含窗口切换/看门狗重启场景），防止 UI 永久 running。"""
    import sqlite3
    con = sqlite3.connect(str(config.META_DB_PATH))
    rows = con.execute(
        "select id from tasks where name like '分钟线回补%' "
        "and status in ('running','pending','cancelling')").fetchall()
    con.close()
    for (tid,) in rows:
        db.finish_task(tid, "cancelled",
                       error="被新回补任务接管（窗口调整/看门狗重启）")
        print(f"[收尾] 旧任务 {tid} 已标记 cancelled", flush=True)


def _spawn_worker(codes: list, start: str, state: Path, log_path: Path):
    codes_file = OUT / "_backfill_codes.json"
    codes_file.write_text(json.dumps(codes), encoding="utf-8")
    venv_py = BACKEND.parent / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    return subprocess.Popen(
        [py, str(WORKER), str(codes_file), str(state), start],
        cwd=str(BACKEND), stdout=log_path.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT)


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _read_state(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _hung_code(st) -> str | None:
    m = _BEFORE_RE.search(str((st or {}).get("m") or ""))
    return m.group(1) if m else None


def _state_done(st, n: int) -> int:
    """state 消息 -> 本轮 worker 已完成票数（AFTER=i 完成i只；BEFORE=i 完成 i-1 只）"""
    msg = str((st or {}).get("m") or "")
    m = _AFTER_RE.search(msg)
    if m:
        return max(0, min(n, int(m.group(2))))
    m = _BEFORE_RE.search(msg)
    if m:
        return max(0, min(n, int(m.group(2)) - 1))
    return 0


def _state_idx(st, n: int) -> int:
    return _state_done(st, n)


def _run_watchdog(task_name: str, target: list, start: str, resume_by_files: bool,
                  msg_prefix: str) -> dict:
    """通用看门狗执行器。

    resume_by_files=True（B 步）：断点 = 无 minute5 文件的票（与源成败无关，
    源失败的票下轮重试）；False（A 步尾部）：断点 = state 消息进度索引。"""
    total = len(target)
    if total == 0:
        return {"done": 0, "total": 0}
    baseline = len(minute5_codes())
    tid = "data_" + uuid.uuid4().hex[:12]
    db.create_task(tid, task_name, "data_update",
                   payload={"scope": "minute5", "codes_n": total,
                            "start_date": start, "watchdog": True})
    db.update_task(tid, status="running")
    print(f"[{task_name}] task={tid} 目标 {total} 只", flush=True)

    skip_path = OUT / "_backfill_skip.json"
    skips: set = set(json.loads(skip_path.read_text(encoding="utf-8"))
                     if skip_path.exists() else [])
    strikes: dict[str, int] = {}
    passes = fails = 0
    done_at_fail = -1
    n_stall_kills = n_recycles = 0
    cursor = 0  # A 模式：target 前缀已处理长度

    while True:
        if resume_by_files:
            have = minute5_codes()
            remaining = [c for c in target if c not in have and c not in skips]
        else:
            remaining = [c for c in target[cursor:] if c not in skips]
        if not remaining:
            break
        state = OUT / "_backfill_state.json"
        state.unlink(missing_ok=True)
        log = OUT / "_backfill_worker.log"
        proc = _spawn_worker(remaining, start, state, log)
        born = time.time()
        last_key = None
        last_move = time.time()
        rc = None
        outcome = "exit"
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            st = _read_state(state)
            nfiles = len(minute5_codes())
            key = ((st or {}).get("m"), nfiles)
            if key != last_key:
                last_key, last_move = key, time.time()
            now = time.time()
            if now - last_move > STALL_SEC:
                outcome = "stall"
                break
            if now - born > RECYCLE_SEC:
                outcome = "recycle"
                break
            cur = ((nfiles - baseline) if resume_by_files
                   else cursor + _state_idx(st, len(remaining)))
            try:
                db.update_progress(tid, round(cur / max(total, 1) * 100),
                                   f"{msg_prefix} ({cur}/{total})")
            except db.TaskCancelled:
                _kill_tree(proc.pid)
                db.finish_task(tid, "cancelled", error="已被用户取消")
                return {"cancelled": True}
            time.sleep(POLL_SEC)

        if rc is None:  # 仍在跑：停滞或轮换 -> 杀树续跑
            hung = _hung_code(_read_state(state))
            _kill_tree(proc.pid)
            if outcome == "stall":
                n_stall_kills += 1
                if hung:
                    strikes[hung] = strikes.get(hung, 0) + 1
                    print(f"[{task_name}] 停滞杀死（疑挂 {hung}，"
                          f"第 {strikes[hung]} 次）", flush=True)
                    if strikes[hung] >= MAX_STRIKES:
                        skips.add(hung)
                        skip_path.write_text(json.dumps(sorted(skips)),
                                             encoding="utf-8")
                        print(f"[{task_name}] {hung} 两次停滞 -> 跳过名单", flush=True)
                else:
                    print(f"[{task_name}] 停滞杀死（未定位挂起票）", flush=True)
            else:
                n_recycles += 1
            if not resume_by_files:
                cursor = min(len(target),
                             cursor + _state_done(_read_state(state), len(remaining)))
            continue

        if rc != 0:  # worker 异常退出：防无限重启（连续3次且无进展 -> 任务失败）
            fails += 1
            done_now = len(minute5_codes()) - baseline
            if fails >= 3 and done_now == done_at_fail:
                db.finish_task(tid, "failed",
                               error=f"worker 连续 {fails} 次异常退出且无进展，"
                                     f"日志: {log}")
                return {"failed": True}
            done_at_fail = done_now
            if not resume_by_files:
                cursor = min(len(target),
                             cursor + _state_done(_read_state(state), len(remaining)))
            continue

        # rc == 0：整轮正常跑完，统计仍未完成的
        passes += 1
        if resume_by_files:
            have = minute5_codes()
            left = [c for c in target if c not in have and c not in skips]
        else:
            cursor = len(target)
            left = []
        if not left:
            break
        if passes >= MAX_FAIL_PASSES:
            skips.update(left)
            skip_path.write_text(json.dumps(sorted(skips)), encoding="utf-8")
            print(f"[{task_name}] {len(left)} 只两轮仍无数据 -> 跳过名单收尾", flush=True)
            break
        print(f"[{task_name}] 本轮结束仍剩 {len(left)} 只，重试一轮", flush=True)

    done = (len(minute5_codes()) - baseline) if resume_by_files else total
    payload = {"done": done, "total": total, "skipped": sorted(skips),
               "stall_kills": n_stall_kills, "recycles": n_recycles}
    try:
        db.update_progress(tid, 100, f"{msg_prefix} 完成({done}/{total})")
    except db.TaskCancelled:
        pass
    db.finish_task(tid, "success", payload=payload)
    print(f"[{task_name}] 完成 {done}/{total}，跳过 {len(skips)}，"
          f"停滞杀死 {n_stall_kills} 次，轮换 {n_recycles} 次", flush=True)
    return payload


def run_a_tail() -> None:
    """A 步：已有票尾部增量（带新鲜度守卫 + 同一看门狗机制）"""
    last = aligned_last_day()
    start_a = (datetime.strptime(last, "%Y-%m-%d")
               + timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    if start_a > today:
        print(f"[A] 对齐日 {last} 已新鲜（start={start_a} > {today}），跳过", flush=True)
        return
    have = minute5_codes()
    target = sorted(have & set(_all_codes()))
    if target:
        _run_watchdog(f"分钟线回补A：尾部增量 {start_a}+", target, start_a,
                      resume_by_files=False, msg_prefix="分钟线尾部")


def _coverage_check(codes_done: list) -> int:
    """B 步收尾：新文件日期覆盖抽查（起点晚于 2024-02 视为疑似截断；
    晚于此的也可能是 2024 后上市新股，软性警告仅供人工复核）"""
    n_bad = 0
    for c in codes_done:
        p = config.MINUTE5_DIR / f"{c}.parquet"
        try:
            dmin = pl.scan_parquet(p).select(pl.col("date").min()).collect().item()
            if str(dmin)[:10] > "2024-02-01":
                n_bad += 1
        except Exception:
            n_bad += 1
    return n_bad


def run_b() -> None:
    have0 = minute5_codes()
    target = [c for c in _all_codes() if c not in have0]
    if not target:
        print("[B] 无缺失票", flush=True)
        return
    stats = _run_watchdog("分钟线回补B：缺失票全窗口（看门狗）", target,
                          BACKFILL_START_B, resume_by_files=True,
                          msg_prefix="分钟线回补")
    if not stats.get("cancelled") and not stats.get("failed"):
        have1 = minute5_codes()
        done_codes = [c for c in target if c in have1]
        n_bad = _coverage_check(done_codes) if done_codes else 0
        if n_bad:
            print(f"[B] 完整性警告：{n_bad} 只新文件起点晚于 2023-04（疑似截断/新股）",
                  flush=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _cancel_stuck_legacy()
    run_a_tail()
    run_b()
    print("回补全部完成", flush=True)


if __name__ == "__main__":
    main()
