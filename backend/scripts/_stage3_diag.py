# -*- coding: utf-8 -*-
"""对账：静态/动态 stage3 各组最优 vs 基座（诊断盲并劣于起点的组最优）。"""
import json
from pathlib import Path

for tag, f in [("static", "stage3_rows.jsonl"), ("dynamic", "stage3D_rows.jsonl")]:
    p = Path(__file__).parent / "out" / f
    if not p.exists():
        print(tag, "no file")
        continue
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    base = next((r for r in rows if r.get("combo") == "BASE_HALF_GATE"), None)
    if base is None:
        print(tag, "no base")
        continue
    bs = base["score"]
    print(f"{tag}: base={bs:.4f}, rows={len(rows)}")
    best = {}
    for r in rows:
        g, rd = r.get("group"), r.get("round")
        if not g:
            continue
        k = (rd, g)
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    for (rd, g), r in sorted(best.items()):
        mark = "  <-- 劣于基座" if r["score"] < bs else ""
        print(f"  r{rd} {g}: {r['score']:.4f}{mark}")
