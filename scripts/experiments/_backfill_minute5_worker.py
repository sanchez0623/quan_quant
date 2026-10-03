# -*- coding: utf-8 -*-
"""分钟线回补 worker：子进程拉取指定票列表（被 _backfill_minute5.py 以子进程驱动）。

进度经 state JSON 文件回报（父进程据此做停滞检测/生命周期轮换/任务进度展示）；
无 DB 依赖（任务由父进程管理）。父进程可随时 taskkill /F /T 杀树——
逐票原子写盘（写完一只落一只），被杀只丢当前票，续跑按"无文件的票"重算。

用法: python _backfill_minute5_worker.py <codes.json路径> <state.json路径> <start_date>"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import updater


def main() -> None:
    codes = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state = Path(sys.argv[2])
    start = sys.argv[3]

    def cb(p: float, m: str) -> None:
        try:  # state 写失败不影响拉取
            state.write_text(json.dumps({"p": p, "m": m, "ts": time.time()}),
                             encoding="utf-8")
        except Exception:
            pass

    updater.update(scope="minute5", codes=codes, progress_cb=cb, start_date=start)


if __name__ == "__main__":
    main()
