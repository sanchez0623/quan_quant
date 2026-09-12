# -*- coding: utf-8 -*-
"""阶段 2 消融验收（V3 寻优方案 docs/OPTIMIZE_SLOT_V3_PLAN.md §5.3.6）。

对照矩阵（11 次回测）：
- B       基座 BT-D1（重跑：校验可复现性 + 取 OOS 切分日 = 全区间 70% 分位）
- FULL    全参数调优（42 项中"最优档好于基座"的参数全部取最优档）
- HALF    砍半配置（仅保留 21 项取最优档，砍掉 21 项固定基座值）
- HALF_PN HALF + pool_n=14（裁决 pool_n 尖峰边界案例：显著优于 HALF 则该回收）
每配置跑 全区间 + OOS 段；另加 随机300 跨池（seed 与 BT-D3 一致）。

判定（方案 §5.3.6 + D3① 标准档精神）：
- 砍半通过：HALF 在 OOS 与全区间上不显著劣于 FULL（score/超额差 < 5pt）
- pool_n 回收：HALF_PN 在 OOS 上显著优于 HALF（差 ≥ 5pt）→ 报告用户拍板规则 v2
- 历史教训检验：FULL 是否赢 B（赢不了说明单参数最优叠加无效，阶段 3 联合寻优更有必要）

最优档来源：stage1_oat_rows.jsonl（阶段 1 OAT 普查结果，唯一数据源，不手抄）。
输出：scripts/out/stage2_ablation_<时间戳>.md。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import random

from stage0_anchors import END_DEFAULT, SEED_DEFAULT, START_DEFAULT, _cfg, _fmt, _zz500_universe  # noqa: E402

from app.engine import runner  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
# 与 stage1_oat.py 勘误 v4 口径一致：起点 2021-07-15（预热自然完成）
ROWS_JSONL = OUT_DIR / "stage1_oat_rows_v4.jsonl"

# 动态语境（--auto）：D-OAT 缓存 + D 版保留名单（stageD_oat_20260912_124506.md 切分）
AUTO = "--auto" in sys.argv
if AUTO:
    ROWS_JSONL = OUT_DIR / "stageD_oat_rows.jsonl"

# 保留名单（阶段 1 v4 报告 stage1_oat_20260908_230404.md 切分预览，21 项）
KEEP = {
    ("params", "pool_n"), ("params", "crash_abs_cap"), ("params", "macd_fast"),
    ("params", "mom_short"), ("params", "ma_fast"), ("params", "macd_signal"),
    ("params", "out_top_days"), ("params", "add_cooldown"), ("params", "exit_need"),
    ("params", "w_accel"), ("params", "w_short"), ("params", "macd_slow"),
    ("params", "crash_vol_n"), ("params", "max_holdings"), ("params", "atr_stop_k"),
    ("params", "exit_confirm_days"), ("params", "add_scale"), ("risk", "stop_loss_mode"),
    ("risk", "adaptive"), ("params", "max_adds"), ("params", "base_pct_max"),
}
if AUTO:
    KEEP = {
        ("params", "w_short"), ("params", "macd_slow"), ("params", "crash_vol_n"),
        ("risk", "atr_multiplier"), ("risk", "stop_loss_mode"), ("params", "ma_fast"),
        ("params", "add_cooldown"), ("params", "max_adds"), ("params", "add_scale"),
        ("params", "max_holdings"), ("params", "add_breakout_n"), ("params", "w_accel"),
        ("params", "w_mid"), ("params", "mom_long"), ("params", "macd_fast"),
        ("params", "base_pct_max"), ("params", "crash_sigma"),
        ("params", "exit_confirm_days"), ("risk", "take_profit_pct"),
        ("params", "mom_short"), ("params", "exit_cooldown"),
    }

# 消融裁决开关：pool_gate（阶段0 收益口径 +18pt vs OAT 超额口径 -0.53 的矛盾）
GATE_KEY = ("top", "pool_gate")

METRIC_KEYS = ["total_return", "annual_return", "benchmark_return", "excess_return",
               "max_drawdown", "sharpe", "calmar", "win_rate"]


def _load_best_overrides() -> dict:
    done = {}
    for line in ROWS_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            done[r["combo"]] = r
        except Exception:
            continue
    base_score = done["BASE"]["score"]
    by_key: dict[tuple[str, str], dict] = {}
    for r in done.values():
        if r.get("key") in (None, "BASE"):
            continue
        k = (r["where"], r["key"])
        if k not in by_key or r["score"] > by_key[k]["score"]:
            by_key[k] = r
    full, half, adopted, kept_adopted = {}, {}, 0, 0
    for k, r in by_key.items():
        if r["score"] > base_score + 1e-9:
            full[k] = r["value"]
            adopted += 1
            if k in KEEP:
                half[k] = r["value"]
                kept_adopted += 1
    print(f"阶段1 普查：采纳档（优于基座）{adopted} 项｜保留名单内采纳 {kept_adopted} 项",
          flush=True)
    return full, half


def _apply_overrides(base_cfg: dict, overrides: dict) -> dict:
    cfg = json.loads(json.dumps(base_cfg))
    for (where, key), v in overrides.items():
        if where == "params":
            cfg["params"][key] = v
        elif where == "risk":
            cfg["risk_config"][key] = v
        else:
            cfg[key] = v
    return cfg


def _split_date(equity_curve: list[dict], ratio: float = 0.7) -> str:
    dates = sorted({str(p.get("date"))[:10] for p in equity_curve})
    return dates[int(len(dates) * ratio) - 1]


def _run(tag: str, cfg: dict, rows: list[dict]) -> dict:
    t0 = time.time()
    rep = runner.run_backtest(cfg)
    m = rep.get("metrics", {}) or {}
    row = {"tag": tag, "name": cfg["name"], "seg": cfg.get("_seg", "full")}
    row.update({k: m.get(k) for k in METRIC_KEYS})
    rows.append(row)
    print(f"  [{tag}] {time.time() - t0:,.0f}s 收益 {_fmt(row['total_return'])} "
          f"| 超额 {_fmt(row['excess_return'])} | 回撤 {_fmt(row['max_drawdown'])} "
          f"| 夏普 {_fmt(row['sharpe'], pct=False)}", flush=True)
    return row


def main():
    t0 = time.time()
    uni_all = _zz500_universe(as_of=START_DEFAULT)
    rng = random.Random(SEED_DEFAULT)
    uni_300 = sorted(rng.sample(uni_all, 300))
    print(f"zz500 {len(uni_all)} 只｜区间 {START_DEFAULT}~{END_DEFAULT}"
          f"｜随机300 seed={SEED_DEFAULT}", flush=True)

    full_ov, half_ov = _load_best_overrides()
    half_gate_ov = dict(half_ov)
    half_gate_ov[GATE_KEY] = True

    base_cfg = _cfg("stage2_base", [] if AUTO else uni_all, universe_auto=AUTO)

    rows: list[dict] = []

    print("[1/4] 全区间：B / FULL / HALF / HALF_GATE ...", flush=True)
    base_rep = runner.run_backtest(base_cfg)
    split = _split_date(base_rep.get("equity_curve") or [])
    m = base_rep.get("metrics", {}) or {}
    brow = {"tag": "B-full", "name": base_cfg["name"], "seg": "full"}
    brow.update({k: m.get(k) for k in METRIC_KEYS})
    rows.append(brow)
    print(f"  [B-full] 收益 {_fmt(brow['total_return'])}｜OOS 切分日 = {split}", flush=True)

    for tag, ov in (("FULL-full", full_ov), ("HALF-full", half_ov),
                    ("HALF_GATE-full", half_gate_ov)):
        cfg = _apply_overrides(base_cfg, ov)
        cfg["name"] = f"stage2_{tag.lower()}"
        _run(tag, cfg, rows)

    print(f"[2/4] OOS 段（{split} ~ {END_DEFAULT}）：B / FULL / HALF / HALF_GATE ...", flush=True)
    for tag, ov in (("B-oos", {}), ("FULL-oos", full_ov), ("HALF-oos", half_ov),
                    ("HALF_GATE-oos", half_gate_ov)):
        cfg = _cfg(f"stage2_{tag.lower()}", [] if AUTO else uni_all,
                   universe_auto=AUTO, start=split, end=END_DEFAULT)
        cfg = _apply_overrides(cfg, ov)
        _run(tag, cfg, rows)

    if AUTO:
        print("[3/4][4/4] 跨池对照跳过（动态语境域固定为 zz500，无换池概念）", flush=True)
    else:
        print("[3/4] 跨池（随机300）：FULL / HALF / HALF_GATE ...", flush=True)
        for tag, ov in (("FULL-pool300", full_ov), ("HALF-pool300", half_ov),
                        ("HALF_GATE-pool300", half_gate_ov)):
            cfg = _cfg(f"stage2_{tag.lower()}", uni_300)
            cfg = _apply_overrides(cfg, ov)
            _run(tag, cfg, rows)

        print("[4/4] 跨池 OOS（随机300）：HALF / HALF_GATE ...", flush=True)
        for tag, ov in (("HALF-pool300-oos", half_ov), ("HALF_GATE-pool300-oos", half_gate_ov)):
            cfg = _cfg(f"stage2_{tag.lower()}", uni_300, start=split, end=END_DEFAULT)
            cfg = _apply_overrides(cfg, ov)
            _run(tag, cfg, rows)

    _report(rows, split, full_ov, half_ov)
    print(f"总耗时 {time.time() - t0:,.0f}s", flush=True)


def _report(rows: list[dict], split: str, full_ov: dict, half_ov: dict) -> None:
    label = {"total_return": "总收益", "annual_return": "年化",
             "benchmark_return": "基准", "excess_return": "超额",
             "max_drawdown": "回撤", "sharpe": "夏普", "calmar": "卡玛",
             "win_rate": "胜率"}
    pct_keys = {"total_return", "annual_return", "benchmark_return", "excess_return",
                "max_drawdown", "win_rate"}
    by = {r["tag"]: r for r in rows}
    lines = [
        "# 阶段 2 消融验收（砍半 vs 全参数）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 全区间 {START_DEFAULT}~{END_DEFAULT}｜OOS = {split}~{END_DEFAULT}"
        f"（70% 切分）｜跨池 = 随机300（seed 20260908）",
        f"- FULL 采纳 {len(full_ov)} 项最优档｜HALF（保留21项内）采纳 {len(half_ov)} 项"
        f"｜HALF_PN = HALF + pool_n=14",
        "",
        "## 对照矩阵",
        "",
        "| 配置 | " + " | ".join(label[k] for k in METRIC_KEYS) + " |",
        "|---|" + "---|" * len(METRIC_KEYS),
    ]
    order = ["B-full", "FULL-full", "HALF-full", "HALF_PN-full",
             "B-oos", "FULL-oos", "HALF-oos", "HALF_PN-oos",
             "FULL-pool300", "HALF-pool300", "HALF_PN-pool300",
             "HALF-pool300-oos", "HALF_PN-pool300-oos"]
    for tag in order:
        r = by.get(tag)
        if not r:
            continue
        cells = [_fmt(r.get(k)) if k in pct_keys else _fmt(r.get(k), pct=False)
                 for k in METRIC_KEYS]
        lines.append(f"| {tag} | " + " | ".join(cells) + " |")

    def ex(tag_):
        r = by.get(tag_)
        return r.get("excess_return") if r else None

    d = lambda a, b: (None if ex(a) is None or ex(b) is None
                      else f"{ex(a) - ex(b):+.2%}")
    lines += ["", "## 判定（阈值：超额差 5pt 为显著）", ""]
    lines.append(f"- **砍半通过性**：HALF vs FULL 超额差 = 全区间 {d('HALF-full','FULL-full')}，"
                 f"OOS {d('HALF-oos','FULL-oos')}，跨池 {d('HALF-pool300','FULL-pool300')}"
                 f"（≥ -5pt 即不显著劣化 → 通过）")
    lines.append(f"- **pool_gate 裁决**：HALF_GATE vs HALF 超额差 = 全区间 "
                 f"{d('HALF_GATE-full','HALF-full')}，OOS {d('HALF_GATE-oos','HALF-oos')}"
                 f"，跨池OOS {d('HALF_GATE-pool300-oos','HALF-pool300-oos')}"
                 f"（OOS ≥ +5pt → gate 该开；看回撤与收益两口径权衡）")
    lines.append(f"- **历史教训检验**：FULL vs B 超额差 = 全区间 {d('FULL-full','B-full')}，"
                 f"OOS {d('FULL-oos','B-oos')}"
                 f"（单参数最优叠加能否赢基座）")
    lines.append(f"- **砍半 vs 基座**：HALF vs B 超额差 = 全区间 {d('HALF-full','B-full')}，"
                 f"OOS {d('HALF-oos','B-oos')}")
    lines += ["", f"- FULL 采纳明细：{json.dumps({f'{w}.{k}': v for (w, k), v in sorted(full_ov.items())}, ensure_ascii=False)}",
              f"- HALF 采纳明细：{json.dumps({f'{w}.{k}': v for (w, k), v in sorted(half_ov.items())}, ensure_ascii=False)}"]
    prefix = "stageD_ablation" if AUTO else "stage2_ablation"
    out = OUT_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
