# -*- coding: utf-8 -*-
"""实验记忆库（方案 B Phase 3b）：历次 AI 分析结论沉淀 + 同策略召回注入。

解决「没有记忆」缺陷：上次建议什么、实测改善还是恶化，这次分析时自动召回，
避免重复踩坑。v1 策略：
- 存储：SQLite ai_memory 表（结论摘要文本 + 可选 embedding JSON）
- 召回：配置了 EMBEDDING_* 环境变量时用向量余弦 top-k；否则降级为同策略最近 N 条
- 写入：ai_analyze_task / ai_refine_task 成功后自动记录一条结论摘要

embedding 配置（可选）：EMBEDDING_API_KEY / EMBEDDING_BASE_URL（默认硅基流动）/
EMBEDDING_MODEL（默认 BAAI/bge-large-zh-v1.5）。任何失败静默降级为文本召回。
"""
import json
import math
import os
from typing import Optional

from .. import db

EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_task_id TEXT,
  backtest_id TEXT,
  strategy_id TEXT,
  text TEXT NOT NULL,
  embedding TEXT,                   -- JSON 数组（配置 embedding 时才有）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_memory_strategy ON ai_memory(strategy_id, created_at);
"""


def init_memory_db(db_path: Optional[str] = None) -> None:
    with db.conn(db_path) as c:
        c.executescript(_SCHEMA)


def _embedding_config() -> Optional[dict]:
    """embedding 可用配置（未配 key 返回 None → 文本召回降级）"""
    key = os.environ.get("EMBEDDING_API_KEY", "").strip()
    if not key:
        return None
    return {"api_key": key,
            "base_url": os.environ.get("EMBEDDING_BASE_URL", "").strip()
            or EMBEDDING_BASE_URL,
            "model": os.environ.get("EMBEDDING_MODEL", "").strip() or EMBEDDING_MODEL}


def embedding_available() -> bool:
    return _embedding_config() is not None


def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """批量 embedding；未配置/失败返回 None（调用方降级为文本召回）"""
    cfg = _embedding_config()
    if not cfg or not texts:
        return None
    try:
        import httpx
        resp = httpx.post(
            cfg["base_url"].rstrip("/") + "/embeddings",
            json={"model": cfg["model"], "input": texts},
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=30, trust_env=False)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return [d["embedding"] for d in sorted(data, key=lambda x: x.get("index", 0))]
    except Exception:  # noqa: BLE001  embedding 是增强项，失败降级
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def record_memory(analysis_task_id: str, backtest_id: str, strategy_id: str,
                  text: str, db_path: Optional[str] = None) -> None:
    """写入一条实验记忆（embedding 可用则同时存向量；失败只存文本）"""
    init_memory_db(db_path)
    emb = embed_texts([text])
    with db.conn(db_path) as c:
        c.execute(
            "INSERT INTO ai_memory(analysis_task_id, backtest_id, strategy_id, "
            "text, embedding, created_at) VALUES(?,?,?,?,?,?)",
            (analysis_task_id, backtest_id, strategy_id, text,
             json.dumps(emb[0]) if emb else None, db._now()))


def build_memory_text(report: dict, suggestions: Optional[dict],
                      validation: Optional[dict]) -> str:
    """从分析要素生成记忆摘要（召回注入与人工查看共用）"""
    cfg = report.get("config") or {}
    m = report.get("metrics") or {}
    comp = (validation or {}).get("comparison") or {}
    parts = [
        f"策略={cfg.get('strategy_id')} 区间={cfg.get('start_date')}~{cfg.get('end_date')}",
        f"原回测收益={m.get('total_return')} 回撤={m.get('max_drawdown')}",
    ]
    if suggestions:
        sug = {**{f"params.{k}": v for k, v in (suggestions.get("params") or {}).items()},
               **{f"risk.{k}": v for k, v in (suggestions.get("risk_config") or {}).items()}}
        parts.append(f"建议={json.dumps(sug, ensure_ascii=False)}")
    if validation:
        parts.append(f"实测verdict={comp.get('verdict')}"
                     + (f" 变好={comp.get('better')} 变差={comp.get('worse')}" if comp.get("verdict") else "")
                     + ("（近空仓化）" if comp.get("conservative") else ""))
    return "；".join(parts)


def recall(strategy_id: Optional[str], query: Optional[str], limit: int = 3,
           db_path: Optional[str] = None,
           exclude_task_id: Optional[str] = None) -> list[dict]:
    """召回同策略历史记忆：embedding 可用 → 向量余弦 top-k；否则最近 N 条。
    exclude_task_id 用于排除当前分析自身（refine 场景）。"""
    init_memory_db(db_path)
    with db.conn(db_path) as c:
        if strategy_id:
            rows = c.execute(
                "SELECT analysis_task_id, text, embedding, created_at FROM ai_memory "
                "WHERE strategy_id=? ORDER BY id DESC LIMIT 200",
                (strategy_id,)).fetchall()
        else:
            rows = c.execute(
                "SELECT analysis_task_id, text, embedding, created_at FROM ai_memory "
                "ORDER BY id DESC LIMIT 200").fetchall()
    rows = [r for r in rows if r[0] != exclude_task_id]
    if not rows:
        return []
    if query and _embedding_config():
        qvecs = embed_texts([query])
        if qvecs:
            scored = []
            for tid, text, emb, created in rows:
                try:
                    vec = json.loads(emb) if emb else None
                except json.JSONDecodeError:
                    vec = None
                score = _cosine(qvecs[0], vec) if vec else 0.0
                scored.append((score, tid, text, created, vec is not None))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [{"analysis_task_id": t, "text": x, "created_at": c,
                     "score": round(sc, 4)} for sc, t, x, c, _ in scored[:limit]]
    return [{"analysis_task_id": r[0], "text": r[1], "created_at": r[3]}
            for r in rows[:limit]]
