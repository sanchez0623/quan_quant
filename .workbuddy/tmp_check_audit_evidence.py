# -*- coding: utf-8 -*-
"""检查审计时刻的物证 c_m5_audit.jsonl"""
import json
from pathlib import Path
p = Path(r"d:\Sanchez\AI\TraeProjects\quan_quant\.workbuddy\c_m5_audit.jsonl")
print("exists:", p.exists())
if p.exists():
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    print("lines:", len(lines))
    marker = json.dumps([])
    nonempty = [l for l in lines if marker not in l]
    print("非空gap行数:", len(nonempty))
    for l in lines[:3]:
        print("  样例:", l[:120])
    for l in nonempty[:3]:
        print("  非空样例:", l[:120])
