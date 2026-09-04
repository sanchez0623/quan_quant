# -*- coding: utf-8 -*-
"""A/B 实验资源盘点：历史回测报告清单 + LLM Key 可用性（不打印密钥）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.llm import provider  # noqa: E402


def main() -> None:
    print("REPORTS_DIR:", config.REPORTS_DIR)
    reports = sorted(Path(config.REPORTS_DIR).glob("bt_*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    print("回测报告总数:", len(reports))
    for p in reports[:15]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            cfg = r.get("config") or {}
            m = r.get("metrics") or {}
            print("{} | {:14s} | {:7s} | {}~{} | 票数:{} | 交易:{} | {}KB | {}".format(
                p.stem, cfg.get("strategy_id") or "?", cfg.get("period") or "?",
                cfg.get("start_date"), cfg.get("end_date"),
                len(cfg.get("universe") or []), m.get("total_trades"),
                p.stat().st_size // 1024, (r.get("name") or "")[:20]))
        except Exception as e:  # noqa: BLE001
            print(p.stem, "读取失败", e)
    pool = provider.parse_key_pool()
    print("环境变量Key池:", [(e["provider"], e["model"]) for e in pool] or "空")
    cfg_llm = provider.load_llm_config()
    for name, p in (cfg_llm.get("profiles") or {}).items():
        print("profile {}: provider={} model={} keys={}".format(
            name, p.get("provider"), p.get("model"), len(provider.profile_api_keys(p))))
    print(".env 存在:", (Path(config.PROJECT_ROOT) / ".env").exists())


if __name__ == "__main__":
    main()
