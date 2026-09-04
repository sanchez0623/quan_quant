# -*- coding: utf-8 -*-
"""A/B 实验：单会话（analyzer.analyze_backtest）vs 多专家并行（panel.run_panel_analysis）。

同一批回测报告、同一规则 findings 输入、同一 LLM Key 池与模型、同一 clamp/验证闭环，
对比：token 成本 / 耗时 / 工具调用 / 建议数量与内容 / 验证回测 verdict。

用法（backend/ 下）：
  python scripts/ab_panel_experiment.py --username admin --profile deepseek --limit 4
  python scripts/ab_panel_experiment.py --task-ids bt_xxx,bt_yyy --skip-validation
输出：scripts/out/ab_panel_result_<时间戳>.md + 控制台摘要。
llm_usage 记录在临时 DB，不污染正式 meta.db。
"""
import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db  # noqa: E402
from app.llm import panel, validation  # noqa: E402
from app.llm.analyzer import analyze_backtest  # noqa: E402
from app.llm.diagnostics import diagnose  # noqa: E402

DEFAULT_TASK_IDS = [
    "bt_3d99bfbe78bb",  # 口径分段·2026年·档1+2
    "bt_04372714c2f9",  # 口径分段·2026年·当前版
    "bt_18a55e91b1df",  # 口径分段·25H2·档1+2
    "bt_60be4a4a89e6",  # 口径分段·25H2·当前版
]


def _load_report(task_id: str) -> dict:
    path = Path(config.REPORTS_DIR) / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"报告不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _sug_info(sug):
    if not sug:
        return 0, {}
    n = len(sug.get("params") or {}) + len(sug.get("risk_config") or {})
    merged = {**{f"params.{k}": v for k, v in (sug.get("params") or {}).items()},
              **{f"risk.{k}": v for k, v in (sug.get("risk_config") or {}).items()}}
    return n, merged


def _validate(report: dict, sug, data_dir: str) -> dict:
    """验证回测（同引擎同区间）；无建议或失败返回占位。"""
    if not sug:
        return {"verdict": "无建议"}
    try:
        v = validation.run_validation_backtest(
            report.get("config") or {}, sug, report.get("metrics") or {},
            data_dir=data_dir)
        return {"verdict": v["comparison"]["verdict"],
                "better": v["comparison"].get("better"),
                "worse": v["comparison"].get("worse")}
    except Exception as e:  # noqa: BLE001
        return {"verdict": None, "error": str(e)[:200]}


def run_one(task_id: str, args, tmp_db: str, data_dir: str) -> dict:
    report = _load_report(task_id)
    findings = diagnose(report)
    row = {"task_id": task_id, "name": report.get("name", ""),
           "trades": (report.get("metrics") or {}).get("total_trades"),
           "findings": [f["code"] for f in findings]}

    # ---- A：单会话（现有生产路径） ----
    t0 = time.time()
    single = analyze_backtest(report, profile=args.profile, db_path=tmp_db,
                              username=args.username, findings=findings,
                              data_dir=data_dir,
                              key_db_path=str(config.META_DB_PATH))
    n1, s1_map = _sug_info(single.get("suggestions"))
    row["single"] = {
        "tokens": single.get("tokens"), "elapsed": round(time.time() - t0, 1),
        "model": single.get("model"),
        "n_tool_calls": len(single.get("tool_trace") or []),
        "n_suggestions": n1, "suggestions": s1_map,
    }
    if not args.skip_validation:
        row["single"]["validation"] = _validate(report, single.get("suggestions"),
                                                data_dir)

    # ---- B：多专家并行 ----
    t0 = time.time()
    multi = panel.run_panel_analysis(report, profile=args.profile, db_path=tmp_db,
                                     username=args.username, findings=findings,
                                     data_dir=data_dir,
                                     key_db_path=str(config.META_DB_PATH))
    n2, s2_map = _sug_info(multi.get("suggestions"))
    row["panel"] = {
        "tokens": multi.get("tokens"), "elapsed": round(time.time() - t0, 1),
        "model": multi.get("model"),
        "lanes_ok": "/".join(r["lane"] for r in multi.get("lanes", [])
                             if r["status"] == "ok"),
        "lane_failed": [r["lane"] for r in multi.get("lanes", [])
                        if r["status"] != "ok"],
        "n_tool_calls": len(multi.get("tool_trace") or []),
        "n_suggestions": n2, "suggestions": s2_map,
    }
    if not args.skip_validation:
        row["panel"]["validation"] = _validate(report, multi.get("suggestions"),
                                               data_dir)
    # 建议重叠率
    set1, set2 = set(s1_map), set(s2_map)
    row["overlap"] = (len(set1 & set2), len(set1 | set2))
    print(f"[done] {task_id} 单会话tokens={row['single']['tokens']} "
          f"多专家tokens={row['panel']['tokens']}", flush=True)
    return row


def render(rows: list, args) -> str:
    lines = [f"# A/B 实验：单会话 vs 多专家并行（{datetime.now():%Y-%m-%d %H:%M}）",
             f"- 模型池: profile={args.profile or 'auto'}，username={args.username}",
             f"- 验证回测: {'跳过' if args.skip_validation else '已执行（同区间同引擎）'}",
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
                         f"模型={d['model']} 下钻={d['n_tool_calls']}次 "
                         f"建议={d['n_suggestions']}项 verdict=**{verdict}**"
                         + (f"（变好:{v.get('better')} 变差:{v.get('worse')}）"
                            if v.get("verdict") in ("改善", "持平", "恶化") else ""))
            if d.get("suggestions"):
                lines.append(f"  - 建议明细: `{json.dumps(d['suggestions'], ensure_ascii=False)}`")
            if mode == "panel":
                lines.append(f"  - 科室完成: {d['lanes_ok']}"
                             + (f"（失败: {d['lane_failed']}）" if d["lane_failed"] else ""))
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
    lines.append("## 汇总")
    lines.append("| 指标 | A 单会话 | B 多专家 |")
    lines.append("|---|---|---|")
    a, b = agg["single"], agg["panel"]
    lines.append(f"| 总 tokens | {a['tokens']:,} | {b['tokens']:,} |")
    lines.append(f"| 总耗时(s) | {a['elapsed']:.0f} | {b['elapsed']:.0f} |")
    lines.append(f"| 下钻次数 | {a['tools']} | {b['tools']} |")
    lines.append(f"| 建议总项数 | {a['sug']} | {b['sug']} |")
    lines.append(f"| verdict 改善/持平/恶化/无 | {a['improved']}/{a['neutral']}/"
                 f"{a['worse']}/{a['none']} | {b['improved']}/{b['neutral']}/"
                 f"{b['worse']}/{b['none']} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", default="admin", help="DB Key 池属主")
    ap.add_argument("--profile", default="deepseek",
                    help="LLM 过滤（服务商名；空=全池轮换）")
    ap.add_argument("--task-ids", default=",".join(DEFAULT_TASK_IDS))
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--skip-validation", action="store_true")
    args = ap.parse_args()

    data_dir = str(config.DATA_DIR)
    tmp_db = str(Path(tempfile.mkdtemp(prefix="ab_eval_")) / "usage.db")
    db.init_db(tmp_db)  # 仅记录本次实验 llm_usage，不污染正式库
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    jsonl = out_dir / "ab_panel_rows.jsonl"   # 增量落盘：每份完成即追加，断点续跑去重
    rows: list[dict] = []
    done: set[str] = set()
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows.append(r)
                done.add(r["task_id"])
    task_ids = [t for t in args.task_ids.split(",") if t][:args.limit]
    todo = [t for t in task_ids if t not in done]
    print(f"A/B 实验：目标 {len(task_ids)} 份，已完成 {len(done & set(task_ids))}，"
          f"待跑 {len(todo)} 份，profile={args.profile}，"
          f"验证回测={'跳过' if args.skip_validation else '执行'}", flush=True)
    for i, tid in enumerate(todo, 1):
        print(f"=== [{i}/{len(todo)}] {tid} ===", flush=True)
        try:
            row = run_one(tid, args, tmp_db, data_dir)
            rows.append(row)
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001  单份失败不拖垮整批
            print(f"[skip] {tid} 失败: {e}", flush=True)
    out = out_dir / f"ab_panel_result_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(render(rows, args), encoding="utf-8")
    stats = db.llm_usage_stats(tmp_db)
    print(f"\n结果已写入: {out}")
    print(f"实验 LLM 消耗: {stats['total_tokens']:,} tokens / {stats['total_calls']} 次调用")


if __name__ == "__main__":
    main()
