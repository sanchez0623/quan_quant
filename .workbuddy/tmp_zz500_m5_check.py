# -*- coding: utf-8 -*-
"""收尾检查：done 是否齐 + 失败段明细"""
import json
from pathlib import Path
from collections import Counter
WORK = Path(r"d:\Sanchez\AI\TraeProjects\quan_quant\.workbuddy")

d = WORK / "zz500_m5_done.txt"
f = WORK / "zz500_m5_failed.jsonl"
need = json.loads((WORK / "zz500_m5_need.json").read_text(encoding="utf-8"))
done = set(d.read_text(encoding="utf-8").split()) if d.exists() else set()
print(f"计划码数: {len(need)} | done: {len(done)} | 未完成码: {len(set(need) - done)}")

fails = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []
print(f"失败段数: {len(fails)} | 涉及码数: {len({x['code'] for x in fails})}")
for x in fails:
    print(" ", x["code"], x["start"], "~", x["end"])
