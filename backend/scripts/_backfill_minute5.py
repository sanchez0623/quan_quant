# -*- coding: utf-8 -*-
"""5分钟线一次性缺口回补（看门狗版 WATCHDOG_BACKFILL）。

背景（2026-09-17 21:14 事故）：B 步全窗口拉取时，个别票 baostock 失败降级
mootdx（TDX 7709），服务器掐断连接（TCP CLOSE_WAIT）后客户端库死循环
（CPU 98%），任务永久挂在 running。本版由父进程驱动 worker 子进程：

- 停滞看门狗：state/文件 8 分钟无变化 -> 杀树重启（taskkill /F /T）
- 生命周期轮换：worker 15 分钟强制回收（连接活不过 ~13 分钟的观察值）
- 挂起计次：同一票 2 次停滞 -> 进跳过名单（scripts/out/_backfill_skip.json）
- 两轮兜底：worker 正常跑完仍无进展的票（源返回空），重试 1 轮后跳过
- 断点续传（A/B 均为文件态，重启零损失）：
  - B 步：missing = 无 minute5 文件的票
  - A 步：文件最大日期 >= 完成线（最近完整交易日 15:00 收盘）即跳过；
    盘中半日数据不算完成（重跑会被完整数据覆盖）
- A 步盘中守卫：交易日 15:05 前跳过尾部增量（半日数据无意义，等收盘后）
- 长期停票剔除：文件最大日期早于最近交易日 45 自然日的票不进 A 目标
  （尾部拉取必然返回空，徒增请求；复活后由每日调度/下次运行补）
- main() 收尾再查一次 A（A 曾因盘中被跳过、B 跑完后已过收盘的场景）
- 数据完整性：B 收尾扫描新文件日期覆盖，疑似截断（起点晚于 2024-02）上报

历史包袱清理：旧"众数对齐日"新鲜度逻辑已弃用——众数翻转（>50% 文件已
更新）会导致重启时误跳 A、剩余文件留洞（2026-09-18 分析确认）。

用法：python scripts/_backfill_minute5.py（建议经 Start-Process 脱离终端启动）
"""
import json
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app import config, db
from app.data import store

BACKFILL_START_B = "2024-01-01"  # 2026-09-17 用户拍板缩窗口：主验证 2024-2025 IS + 2026 OOS；2023 跨窗段退化为日线级验证
STALL_SEC = 480        # 停滞判定：state/文件 8 分钟无变化（正常节奏最长 ~130s/票）
RECYCLE_SEC = 900      # worker 生命周期上限 15 分钟（观察值：mootdx 连接 ~13 分钟被掐）
POLL_SEC = 15
MAX_STRIKES = 2        # 同一票停滞达 2 次 -> 跳过名单
MAX_FAIL_PASSES = 2    # 整轮正常跑完仍无进展的票：最多 2 轮后跳过
A_ACTIVE_WINDOW_DAYS = 45  # A 目标活跃线：文件最大日期早于该自然日数的长期停票剔除
OUT = BACKEND / "scripts" / "out"
WORKER = BACKEND / "scripts" / "_backfill_minute5_worker.py"

_BEFORE_RE = re.compile(r"正在拉取分钟线:\s*(\d{6})\s*\((\d+)/")
_AFTER_RE = re.compile(r"分钟线(?:完成|拉取失败\(跳过\)):\s*(\d{6})\s*\((\d+)/")


def minute5_codes() -> set:
    return {p.stem for p in config.MINUTE5_DIR.glob("*.parquet")}


def _minute5_max_map() -> dict[str, str]:
    """code -> 文件内最大日期（YYYY-MM-DD HH:MM）。A 步逐票断点判定的数据源；
    全库元数据扫描（~3800 文件并行 8 线程），仅在断点重算时调用（每轮换一次）。"""
    files = sorted(config.MINUTE5_DIR.glob("*.parquet"))

    def _mx(fp: Path):
        try:
            return pl.scan_parquet(fp).select(pl.col("date").max()).collect().item()
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        vals = list(ex.map(_mx, files))
    return {fp.stem: str(v) for fp, v in zip(files, vals) if v}


def _last_trade_day(today: str) -> str:
    """日历里 <= today 的最后交易日；无日历按工作日兜底"""
    cal = store.read_calendar()
    if cal is not None and cal.height:
        open_days = (cal.filter((pl.col("date") <= today)
                                & (pl.col("is_open").cast(pl.Int8) == 1))
                     .sort("date"))
        if open_days.height:
            return str(open_days["date"][-1])
    d = datetime.strptime(today, "%Y-%m-%d")
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


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
    """state 消息 -> 本轮 worker 已完成票数（AFTER=i 完成i只；BEFORE=i 完成 i-1 只）。
    仅用于进度条显示，断点判定不依赖它（文件态才是事实源）。"""
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


def _run_watchdog(task_name: str, target: list, start: str, remaining_fn,
                  msg_prefix: str) -> dict:
    """通用看门狗执行器（A/B 共用，断点全部由 remaining_fn 文件态判定）。

    remaining_fn()：返回当前仍未完成的票（纯文件态，重启零损失）——
    A 步 = 文件最大日期未达完成线；B 步 = 无 minute5 文件。"""
    total = len(target)
    if total == 0:
        return {"done": 0, "total": 0}
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

    while True:
        raw = remaining_fn()   # 未完成全集（含跳过名单内的票）
        remaining = [c for c in raw if c not in skips]
        if not remaining:
            break
        done_base = total - len(raw)
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
            cur = done_base + _state_idx(st, len(remaining))
            try:
                db.update_progress(tid, round(cur / max(total, 1) * 100),
                                   f"{msg_prefix} ({cur}/{total})")
            except db.TaskCancelled:
                _kill_tree(proc.pid)
                db.finish_task(tid, "cancelled", error="已被用户取消")
                return {"cancelled": True}
            time.sleep(POLL_SEC)

        if rc is None:  # 停滞或轮换 -> 杀树，remaining_fn 重算断点续跑
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
            continue

        if rc != 0:  # worker 异常退出：防无限重启（连续3次且无进展 -> 任务失败）
            fails += 1
            done_now = total - len(remaining_fn())
            if fails >= 3 and done_now == done_at_fail:
                db.finish_task(tid, "failed",
                               error=f"worker 连续 {fails} 次异常退出且无进展，"
                                     f"日志: {log}")
                return {"failed": True}
            done_at_fail = done_now
            continue

        # rc == 0：整轮正常跑完，统计仍未完成的
        passes += 1
        left = [c for c in remaining_fn() if c not in skips]
        if not left:
            break
        if passes >= MAX_FAIL_PASSES:
            skips.update(left)
            skip_path.write_text(json.dumps(sorted(skips)), encoding="utf-8")
            print(f"[{task_name}] {len(left)} 只两轮仍无数据 -> 跳过名单收尾", flush=True)
            break
        print(f"[{task_name}] 本轮结束仍剩 {len(left)} 只，重试一轮", flush=True)

    done = total - len(remaining_fn())
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
    """A 步：已有票尾部增量（逐票文件断点）。

    完成线 = 最近完整交易日 15:00 收盘 bar。半日数据（盘中拉取）不算完成，
    重跑覆盖为完整数据；已完整的票重启后直接跳过，杜绝旧"众数对齐日"
    翻转误跳 + A 内存游标清零重跑两个问题。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    ltd = _last_trade_day(today)
    if ltd == today and (now.hour * 60 + now.minute) < 15 * 60 + 5:
        print(f"[A] 盘中 {now:%H:%M}——当日数据未完整，跳过尾部增量"
              f"（收盘后重跑或夜间调度补全）", flush=True)
        return
    done_marker = f"{ltd} 15:00"
    have = minute5_codes()
    all_codes = set(_all_codes())
    mx = _minute5_max_map()
    cutoff = (datetime.strptime(ltd, "%Y-%m-%d")
              - timedelta(days=A_ACTIVE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    # 活跃目标：近期有数据的已有文件（长期停票剔除）
    target = sorted(c for c in (have & all_codes) if mx.get(c, "")[:10] >= cutoff)
    n_stale = sum(1 for c in target if mx.get(c, "") < done_marker)
    if n_stale == 0:
        print(f"[A] 活跃票全部已覆盖到 {done_marker}，跳过", flush=True)
        return

    def _remaining() -> list:
        m = _minute5_max_map()
        return [c for c in target if m.get(c, "") < done_marker]

    # 窗口起点 = 未完成票中最旧的最大日期 + 1 天（半日/隔日缺口一并补齐）
    start_a = (datetime.strptime(
        min(mx.get(c, "")[:10] for c in target if mx.get(c, "") < done_marker),
        "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[A] 完成线 {done_marker}：待补 {n_stale}/{len(target)} 只，窗口 {start_a}+",
          flush=True)
    _run_watchdog(f"分钟线回补A：尾部增量 {start_a}+", target, start_a,
                  remaining_fn=_remaining, msg_prefix="分钟线尾部")


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

    def _remaining() -> list:
        have = minute5_codes()
        return [c for c in target if c not in have]

    stats = _run_watchdog("分钟线回补B：缺失票全窗口（看门狗）", target,
                          BACKFILL_START_B, remaining_fn=_remaining,
                          msg_prefix="分钟线回补")
    if not stats.get("cancelled") and not stats.get("failed"):
        have1 = minute5_codes()
        done_codes = [c for c in target if c in have1]
        n_bad = _coverage_check(done_codes) if done_codes else 0
        if n_bad:
            print(f"[B] 完整性警告：{n_bad} 只新文件起点晚于 2024-02（疑似截断/新股）",
                  flush=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _cancel_stuck_legacy()
    run_a_tail()
    run_b()
    run_a_tail()   # B 跑完后兜底再查一次（A 曾因盘中被跳过、此时已过收盘的场景）
    print("回补全部完成", flush=True)


if __name__ == "__main__":
    main()
