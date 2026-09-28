# -*- coding: utf-8 -*-
"""9·25 事故存量数据修补（幂等，可重复执行；默认预览，--apply 落库）。

背景：前端回填成交不带 fill_time -> sig_fills.fill_time / sig_position.open_day
落 NULL -> 盘中 T+1 拦截失效；且误报的「槽位轮动」清仓信号已把状态机复位，
虚拟持仓与策略大脑脱钩。本脚本修四件事：

1. sig_fills.fill_time 缺失 -> 补 created_at（回填时刻近似成交时刻）
2. sig_position.open_day 缺失 -> 该票最早一笔 buy 的 fill_time 日期，
   无成交流水退 updated_at 日期
3. 有虚拟持仓(volume>0)但状态机 opened!=1 -> 重建外置建仓态
   （opened/full 置位 + last_new_high_idx=-1 标记，SlotStepper.step 首喂
   bar 自愈新高基准；last_bar 游标原样保留）
4. 待执行的「槽位轮动」清仓信号且票仍持仓、信号当天==open_day（当日买入
   当日清的事故签名）-> 置 已忽略（防误执行）

用法：
    python scripts/_fix_live_open_day.py            # 预览
    python scripts/_fix_live_open_day.py --apply    # 落库（先备份 meta.db）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

MARKER_ST = {"opened": 1, "full": 1, "adds_done": 0, "last_add_idx": -10**9,
             "exit_stage": 0, "has_reduced": 0, "fade_streak": 0,
             "mom_state": "cruise", "fade_today": 0,
             "last_new_high_idx": -1, "high_since_open": None}


def main(apply: bool) -> None:
    print(f"=== 9·25 事故存量修补 apply={apply} db={db.DEFAULT_DB} ===")
    with db.conn() as c:
        fills = c.execute(
            "SELECT id, code, side, created_at FROM sig_fills "
            "WHERE fill_time IS NULL OR fill_time = ''").fetchall()
        print(f"[1] sig_fills 缺 fill_time: {len(fills)} 行")
        for fid, code, side, created in fills:
            print(f"    fill#{fid} {code} {side} -> fill_time={created}")
        if apply:
            c.execute("UPDATE sig_fills SET fill_time = created_at "
                      "WHERE fill_time IS NULL OR fill_time = ''")

        positions = c.execute(
            "SELECT code, name, volume, updated_at FROM sig_position "
            "WHERE open_day IS NULL OR open_day = ''").fetchall()
        print(f"[2] sig_position 缺 open_day: {len(positions)} 行")
        for code, name, vol, updated in positions:
            row = c.execute(
                "SELECT MIN(fill_time) FROM sig_fills "
                "WHERE code = ? AND side = 'buy' AND fill_time IS NOT NULL",
                (code,)).fetchone()
            day = (row[0] or updated or "")[:10] or None
            print(f"    {code} {name} x{vol} -> open_day={day}")
            if apply and day:
                c.execute("UPDATE sig_position SET open_day = ? "
                          "WHERE code = ?", (day, code))

        held = c.execute(
            "SELECT code, name, volume FROM sig_position "
            "WHERE volume > 0").fetchall()
        held_codes = [r[0] for r in held]
        print(f"[3] 有持仓但状态机未置位（重建外置建仓态）:")
        n3 = 0
        rebuilds = []
        for code, name, vol in held:
            row = c.execute(
                "SELECT st_json, last_bar FROM sig_strategy_state "
                "WHERE code = ?", (code,)).fetchone()
            st = json.loads(row[0]) if row and row[0] else {}
            if st.get("opened"):
                continue
            st.update(MARKER_ST)
            n3 += 1
            rebuilds.append((code, st, row[1] if row else None))
            print(f"    {code} {name} x{vol} -> opened=1 "
                  f"(last_new_high_idx=-1 交由 step 首喂自愈, "
                  f"last_bar={row[1] if row else None})")
        if not n3:
            print("    （无）")

        print(f"[4] 待执行「槽位轮动」清仓且票仍持仓、信号当天==open_day "
              f"（事故签名）-> 已忽略:")
        n4 = 0
        for sid, code, ts in c.execute(
                "SELECT id, code, ts FROM sig_signal_log "
                "WHERE kind = 'intraday' AND stype = '清仓' "
                "AND status = '待执行' AND reason LIKE '槽位轮动%'"
        ).fetchall():
            od = c.execute(
                "SELECT open_day FROM sig_position WHERE code = ?",
                (code,)).fetchone()
            if not od or not od[0] or od[0] != (ts or "")[:10]:
                continue
            n4 += 1
            print(f"    信号#{sid} {code} {ts} -> 已忽略")
            if apply:
                c.execute("UPDATE sig_signal_log SET status = '已忽略' "
                          "WHERE id = ?", (sid,))
        if not n4:
            print("    （无）")

    if not apply:
        print("=== 预览结束（未落库；加 --apply 执行）===")
        return
    # 状态重建放在事务提交之后：save_strategy_state 是独立连接，
    # 嵌在未提交事务内会撞上自身写锁（WAL 单写者）
    for code, st, last_bar in rebuilds:
        db.save_strategy_state(code, st, last_bar)
    print("=== 修补完成 ===")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
