# -*- coding: utf-8 -*-
"""修复收尾：从原始备份合并非域内股票因子 + 修复后健康检查。"""
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import store  # noqa: E402
from app.data.adj_health import check_adj_health, fmt_health_report  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data"
ORIG_BACKUP = DATA / "adj_factor_backup_20260911_164747.parquet"  # 首次备份=原始完整表
repaired = pl.read_parquet(DATA / "adj_factor.parquet")
orig = pl.read_parquet(ORIG_BACKUP)
fixed_codes = repaired["code"].unique().to_list()

merged = pl.concat([
    orig.filter(~pl.col("code").is_in(fixed_codes)),
    repaired,
]).sort(["code", "date"])
store.write_adj_factor(merged, data_dir=None)
print(f"合并: 原始备份 {orig.height} 行 + 修复 {repaired.height} 行 -> {merged.height} 行")

h = check_adj_health(data_dir=None)
print(fmt_health_report(h))
