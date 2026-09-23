# -*- coding: utf-8 -*-
"""补洞 worker：按票区段定向拉取并合并回 minute5 文件。

用法: python _backfill_m5_holes_worker.py <jobs.json路径> <state.json路径>
jobs: [{"code": "688008", "segments": [["2024-02-01", "2024-08-15"], ...]}]

- 数据源 baostock 优先（深历史，能覆盖科创板 2024H1）-> mootdx 兜底
- 每票汇总所有区段帧后一次性读-合并-写（unique(code,date) keep=last，幂等）
- 合并口径与 updater 一致：volume/amount 对齐库内 dtype 再 concat
"""
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import polars as pl

from app.data import sources, store


def main() -> None:
    # 开工前检查 baostock 黑名单
    from app.data.bs_usage import tracker as _bs_tracker
    if _bs_tracker.is_blacklisted():
        info = _bs_tracker.last_blacklist() or {}
        release = info.get("release_at") or "稍后"
        print(f"[阻断] baostock IP 黑名单限制中，预计 {release} 解除，退出", flush=True)
        return
    jobs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state = Path(sys.argv[2])
    n = len(jobs)
    srcs = [s for s in sources.SOURCES
            if type(s).__name__ in ("BaostockSource", "MootdxSource")]

    def report(m: str) -> None:
        try:
            state.write_text(json.dumps({"m": m, "ts": time.time()}),
                             encoding="utf-8")
        except Exception:
            pass

    for i, job in enumerate(jobs):
        code = job["code"]
        report(f"补洞: {code} ({i + 1}/{n})")
        frames = []
        for s, e in job["segments"]:
            df = None
            for src in srcs:
                try:
                    df = src.get_minute5(code, s, e)
                except Exception:
                    df = None
                if df is not None and df.height:
                    break
                df = None
            if df is not None and df.height:
                frames.append(df)
        if frames:
            new = pl.concat(frames)
            existing = store.read_minute5(code)
            if existing is not None and existing.height:
                for col in ("volume", "amount"):
                    if col in new.columns and col in existing.columns:
                        new = new.with_columns(
                            pl.col(col).cast(existing[col].dtype))
                new = (pl.concat([existing, new])
                       .unique(subset=["code", "date"], keep="last")
                       .sort("date"))
            store.write_minute5(code, new)
            report(f"补洞完成: {code} ({i + 1}/{n})")
        else:
            report(f"补洞失败(跳过): {code} ({i + 1}/{n})")
    report(f"worker 完成 ({n}/{n})")


if __name__ == "__main__":
    main()
