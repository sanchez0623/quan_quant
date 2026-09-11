# -*- coding: utf-8 -*-
"""数据治理 L5：复权因子一键修复工具。

对目标 universe 逐股用 akshare 日级直连（hfq/raw 比值，全历史连续、无事件
展开边界）重拉因子；拉取失败保留旧值并记 needs_refetch；修复前后各跑一次
L4 健康检查输出对比报告。备份旧 adj_factor.parquet。

用法（backend/ 下）：
  python scripts/repair_adj_factor.py                     # 默认 v4 域（2021-06-14 快照 500 只）
  python scripts/repair_adj_factor.py --all               # 全库（约 5261 只，耗时较长）
  python scripts/repair_adj_factor.py --snapshot 2021-06-14 --subset 150
输出：scripts/out/repair_adj_<时间戳>.md；因子表就地更新（旧表备份）。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import sources, store  # noqa: E402
from app.data.adj_health import check_adj_health, fmt_health_report  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
DATA = Path(__file__).resolve().parents[2] / "data"


def universe_codes(snapshot: str | None, all_codes: bool) -> list[str]:
    if all_codes:
        daily = store.read_daily(data_dir=None)
        return sorted(daily["code"].unique().to_list())
    hist = pl.read_parquet(DATA / "index_constituents_history.parquet")
    zz = hist.filter(pl.col("index_key") == "zz500")
    snaps = sorted(zz["snap_date"].unique().to_list())
    snap = snapshot or snaps[-1]
    hz = zz.filter(pl.col("snap_date") <= snap)
    snap = hz["snap_date"].max()
    codes = sorted(set(hz.filter(pl.col("snap_date") == snap)["code"].to_list()))
    print(f"修复域: zz500 历史快照 {snap}（{len(codes)} 只）", flush=True)
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None, help="zz500 历史快照日（默认最近一期）")
    ap.add_argument("--all", action="store_true", help="修复全库（默认仅 v4 快照域）")
    ap.add_argument("--sleep", type=float, default=0.35, help="请求间隔秒（防封）")
    args = ap.parse_args()

    t0 = time.time()
    OUT_DIR.mkdir(exist_ok=True)
    ak = next((s for s in sources.SOURCES if s.name == "akshare" and s.available()), None)
    if ak is None:
        raise SystemExit("akshare 源不可用（检查安装与网络）")
    print(f"修复源: akshare 日级直连（hfq/raw 比值）", flush=True)

    codes = universe_codes(args.snapshot, args.all)
    before = check_adj_health(data_dir=None)
    print(fmt_health_report(before), flush=True)

    # 备份（在修复前健康检查之后）
    adj_path = DATA / "adj_factor.parquet"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DATA / f"adj_factor_backup_{ts}.parquet"
    adj_path.replace(backup)
    print(f"旧因子表已备份 -> {backup.name}", flush=True)

    ok_rows: list[pl.DataFrame] = []
    repaired, kept_old, refetch = 0, 0, []
    progress_path = OUT_DIR / "repair_adj_done.json"
    done: set = set()
    if progress_path.exists():
        done = set(json.loads(progress_path.read_text()))
        print(f"续跑: 已完成 {len(done)} 只", flush=True)

    total = len(codes)
    for i, code in enumerate(codes):
        if code in done:
            continue
        df = None
        for attempt in (1, 2):
            try:
                df = ak.get_adj_factor(code)
            except Exception:
                df = None
            if df is not None and df.height:
                break
            time.sleep(args.sleep * 2)
        if df is not None and df.height:
            ok_rows.append(df)
            repaired += 1
        else:
            refetch.append(code)
            kept_old += 1
        done.add(code)
        if i % 25 == 0 or i == total - 1:
            progress_path.write_text(json.dumps(sorted(done)))
            print(f"  [{i + 1}/{total}] 已修复 {repaired}｜失败 {len(refetch)} "
                  f"({time.time() - t0:,.0f}s)", flush=True)
        time.sleep(args.sleep)

    # 整股替换语义：成功股删除旧行插入新行；失败股保留旧值
    existing = pl.read_parquet(backup)
    new_part = (pl.concat(ok_rows, how="diagonal_relaxed")
                if ok_rows else pl.DataFrame({"code": [], "date": [], "adj_factor": []}))
    fixed_codes = set(new_part["code"].unique().to_list()) if new_part.height else set()
    out = pl.concat([
        existing.filter(~pl.col("code").is_in(sorted(fixed_codes))),
        new_part,
    ]).sort(["code", "date"])
    store.write_adj_factor(out, data_dir=None)

    after = check_adj_health(data_dir=None)
    lines = [
        "# 复权因子修复报告",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 修复域: {total} 只｜成功重拉 {repaired}｜失败保留旧值 {kept_old}"
        f"｜备份 {backup.name}",
        "",
        "## 修复前健康检查",
        "",
        "```", fmt_health_report(before), "```",
        "",
        "## 修复后健康检查",
        "",
        "```", fmt_health_report(after), "```",
        "",
        "## needs_refetch（拉取失败名单）",
        "",
        ", ".join(refetch) if refetch else "无",
    ]
    out_md = OUT_DIR / f"repair_adj_{ts}.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 -> {out_md}", flush=True)
    print(f"总耗时 {time.time() - t0:,.0f}s", flush=True)


if __name__ == "__main__":
    main()
