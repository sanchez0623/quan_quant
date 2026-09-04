# -*- coding: utf-8 -*-
"""A/B 实验结果汇总：读 jsonl 明细，输出最终 markdown 报告 + 控制台摘要"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path(__file__).resolve().parent / "out"
rows = [json.loads(l) for l in (OUT / "ab_panel_rows.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()]

lines = [f"# A/B 实验结果：单会话 vs 多专家并行（{datetime.now():%Y-%m-%d %H:%M} 汇总）",
         "- 模型：deepseek-v4-flash（admin DB Key 池，profile=deepseek）",
         "- 共同输入：同一份报告 + 同一规则引擎 findings + 同一 clamp 护栏 + 同一验证闭环",
         ""]
agg = {k: {"tokens": 0, "elapsed": 0.0, "tools": 0, "sug": 0,
           "improved": 0, "worse": 0, "neutral": 0, "none": 0}
       for k in ("single", "panel")}
for r in rows:
    lines.append(f"## {r['task_id']} {r['name']}（交易 {r['trades']} 笔）")
    lines.append(f"- 规则 findings: {', '.join(r['findings']) or '无'}")
    for mode, label in (("single", "A 单会话"), ("panel", "B 多专家")):
        d = r[mode]
        v = d.get("validation") or {}
        verdict = v.get("verdict") or ("失败" if v.get("error") else "-")
        lines.append(f"- **{label}**：tokens={d['tokens']} 耗时={d['elapsed']}s "
                     f"下钻={d['n_tool_calls']}次 建议={d['n_suggestions']}项 "
                     f"verdict=**{verdict}**"
                     + (f"（变好:{v.get('better')}｜变差:{v.get('worse')}）"
                        if v.get("verdict") in ("改善", "持平", "恶化") else ""))
        if d.get("suggestions"):
            lines.append(f"  - 建议明细: `{json.dumps(d['suggestions'], ensure_ascii=False)}`")
        if mode == "panel":
            lanes = d.get("lanes_ok", "")
            failed = d.get("lane_failed") or []
            lines.append(f"  - 科室完成: {lanes}" + (f"（失败: {failed}）" if failed else ""))
    o1, o2 = r["overlap"]
    lines.append(f"- 建议重叠: {o1}/{o2} 项")
    lines.append("")
    for mode in ("single", "panel"):
        d = r[mode]
        a = agg[mode]
        a["tokens"] += d["tokens"] or 0
        a["elapsed"] += d["elapsed"] or 0
        a["tools"] += d["n_tool_calls"]
        a["sug"] += d["n_suggestions"]
        verdict = (d.get("validation") or {}).get("verdict")
        if verdict == "改善":
            a["improved"] += 1
        elif verdict == "恶化":
            a["worse"] += 1
        elif verdict == "持平":
            a["neutral"] += 1
        else:
            a["none"] += 1
a, b = agg["single"], agg["panel"]
lines += ["## 汇总（4 份报告）", "", "| 指标 | A 单会话 | B 多专家 |", "|---|---|---|",
          f"| 总 tokens | {a['tokens']:,} | {b['tokens']:,} |",
          f"| 总耗时(s) | {a['elapsed']:.0f} | {b['elapsed']:.0f} |",
          f"| 下钻次数 | {a['tools']} | {b['tools']} |",
          f"| 建议总项数 | {a['sug']} | {b['sug']} |",
          f"| verdict 改善/持平/恶化/无 | {a['improved']}/{a['neutral']}/{a['worse']}/{a['none']} "
          f"| {b['improved']}/{b['neutral']}/{b['worse']}/{b['none']} |", ""]
out = OUT / f"ab_panel_final_{datetime.now():%Y%m%d_%H%M%S}.md"
out.write_text("\n".join(lines), encoding="utf-8")
print("written:", out)
print()
print("\n".join(lines))
