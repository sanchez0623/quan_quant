# -*- coding: utf-8 -*-
"""大盘闸门 MA 年度稳健性验证（方案：MA30/MA60 × 2022-2025 各年度窗口 vs 基座闸门关）。

12 回测 = 基座×4 + MA30×4 + MA60×4。
判定：MA30/MA60 年度胜率 ≥3/4 且年度 Δ超额均值 > 0 → 稳健性确认；
否则单段（全区间+OOS 双段）幻觉确认，不采纳。
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_gateoff_oat import base_gate_on  # noqa: E402
from pulse_ratchet_oat import run_score  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
ROWS_JSONL = OUT_DIR / "pulse_gatema_year_rows.jsonl"
WINDOWS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
]
MAS = [("基座(关)", None), ("MA30", 30), ("MA60", 60)]


def mk(cfg: dict, ma, win_tag: str, s: str, e: str) -> dict:
    out = json.loads(json.dumps(cfg))
    if ma is not None:
        out["index_gate"] = True
        out["index_gate_ma"] = ma
    out["start_date"] = s
    out["end_date"] = e
    label = "base" if ma is None else f"ma{ma}"
    out["name"] = f"gatema_y_{label}_{win_tag}"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adopt", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    OUT_DIR.mkdir(exist_ok=True)

    done: dict[str, dict] = {}
    if ROWS_JSONL.exists():
        for line in ROWS_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done[r["combo"]] = r
            except Exception:
                continue

    def run_and_log(cfg: dict, combo: str, desc: str) -> dict:
        if combo in done:
            print(f"[缓存] {desc}", flush=True)
            return done[combo]
        print(f"[回测] {desc} ...", flush=True)
        r = run_score(cfg)
        r["combo"] = combo
        done[combo] = r
        with ROWS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  → {desc}: 超额 {_pct(r.get('excess_return'))}｜"
              f"回撤 {_pct(r.get('max_drawdown'))}", flush=True)
        return r

    def _pct(v):
        return f"{v:+.2%}" if isinstance(v, (int, float)) else "-"

    cfg0 = base_gate_on()
    for win_tag, s, e in WINDOWS:
        for label, ma in MAS:
            combo = f"{label}_{win_tag}"
            run_and_log(mk(cfg0, ma, win_tag, s, e), combo,
                        f"{label} {win_tag}年")

    # 年度对照表
    rows = []
    wins30 = wins60 = 0
    d30s, d60s = [], []
    for win_tag, _s, _e in WINDOWS:
        b = done.get(f"基座(关)_{win_tag}")
        m30 = done.get(f"MA30_{win_tag}")
        m60 = done.get(f"MA60_{win_tag}")
        be = b.get("excess_return") if b else None
        e30 = m30.get("excess_return") if m30 else None
        e60 = m60.get("excess_return") if m60 else None
        d30 = (e30 - be) if (e30 is not None and be is not None) else None
        d60 = (e60 - be) if (e60 is not None and be is not None) else None
        if d30 is not None:
            d30s.append(d30)
            wins30 += 1 if d30 > 0 else 0
        if d60 is not None:
            d60s.append(d60)
            wins60 += 1 if d60 > 0 else 0
        rows.append((win_tag, be, e30, d30, e60, d60))
    _report(rows, wins30, wins60, d30s, d60s)
    print(f"总耗时 {time.time()-t0:,.0f}s", flush=True)


def _report(rows, wins30, wins60, d30s, d60s) -> None:
    lines = [
        "# 大盘闸门 MA 年度稳健性验证（MA30/MA60 vs 基座闸门关，2022-2025 分年）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 基座 = bt_76889c798212 形态｜判定：年度胜率 ≥3/4 且 Δ超额均值>0 → 稳健",
        "",
        "| 年度 | 基座超额 | MA30超额 | MA30 Δ | MA60超额 | MA60 Δ |",
        "|---|---|---|---|---|---|",
    ]
    for win_tag, be, e30, d30, e60, d60 in rows:
        def f(v):
            return f"{v:+.2%}" if isinstance(v, (int, float)) else "-"
        lines.append(
            f"| {win_tag} | {f(be)} | {f(e30)} | {f(d30)} | {f(e60)} | {f(d60)} |")
    m30 = sum(d30s) / len(d30s) if d30s else 0
    m60 = sum(d60s) / len(d60s) if d60s else 0
    lines += [
        "",
        f"- **MA30**：年度胜率 {wins30}/{len(d30s)}｜Δ超额均值 {m30:+.2%}",
        f"- **MA60**：年度胜率 {wins60}/{len(d60s)}｜Δ超额均值 {m60:+.2%}",
        "",
        "## 判定",
        "",
        "- 胜率 ≥3/4 且均值>0 → 年度稳健性确认（结合全区间+OOS 双段超额证据采纳）；",
        "- 否则判定全区间+OOS 双段为特定窗口组合的幻觉，维持闸门关。",
    ]
    out = OUT_DIR / f"pulse_gatema_yearly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告 → {out}", flush=True)


if __name__ == "__main__":
    main()
