# -*- coding: utf-8 -*-
"""SQLite 元数据层：users / tasks / backtest_reports / ai_analyses / llm_usage
每次操作新建连接（短事务），WAL 模式保证多进程（进程池worker）读写安全。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

from . import config

# 进程池 worker 会传 db_path，主进程用默认值
DEFAULT_DB = str(config.META_DB_PATH)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY,
  name TEXT DEFAULT '',
  type TEXT DEFAULT 'backtest',
  status TEXT DEFAULT 'pending',
  progress REAL DEFAULT 0,
  message TEXT DEFAULT '',
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  payload TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type, created_at);
CREATE TABLE IF NOT EXISTS backtest_reports(
  task_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_analyses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  backtest_id TEXT,
  profile TEXT,
  model TEXT,
  status TEXT,
  content TEXT,
  tokens_used INTEGER,
  elapsed REAL,
  error TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  elapsed REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,           -- 属主：每人只看到自己的 key
  provider TEXT NOT NULL,           -- deepseek/openrouter/volc/zhipu/siliconflow/ollama/custom
  model TEXT,                       -- 空 = 用内置默认模型
  base_url TEXT,                    -- custom 时必填；其余用内置注册表
  api_key TEXT NOT NULL,
  label TEXT DEFAULT '',            -- 备注名
  sort_order INTEGER DEFAULT 0,     -- 轮换优先级（小者先）
  enabled INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_keys_user ON llm_keys(username, sort_order);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def conn(db_path: Optional[str] = None):
    """短事务连接：WAL + busy_timeout"""
    c = sqlite3.connect(db_path or DEFAULT_DB, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db(db_path: Optional[str] = None) -> None:
    p = db_path or DEFAULT_DB
    from pathlib import Path
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with conn(p) as c:
        c.executescript(_SCHEMA)


# ---------------- users ----------------

def create_user(username: str, password_hash: str, db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute("INSERT OR IGNORE INTO users(username, password_hash, created_at) VALUES(?,?,?)",
                  (username, password_hash, _now()))


def get_user(username: str, db_path: Optional[str] = None) -> Optional[dict]:
    with conn(db_path) as c:
        row = c.execute("SELECT id, username, password_hash FROM users WHERE username=?", (username,)).fetchone()
    return {"id": row[0], "username": row[1], "password_hash": row[2]} if row else None


def list_users(db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
    return [{"id": r[0], "username": r[1], "created_at": r[2]} for r in rows]


def update_user_password(username: str, password_hash: str, db_path: Optional[str] = None) -> bool:
    with conn(db_path) as c:
        cur = c.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash, username))
    return cur.rowcount > 0


def delete_user(username: str, db_path: Optional[str] = None) -> bool:
    """删除用户并级联删除其 LLM keys；禁止删除管理员账号"""
    if username == config.ADMIN_USERNAME:
        return False
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM users WHERE username=?", (username,))
        c.execute("DELETE FROM llm_keys WHERE username=?", (username,))
    return cur.rowcount > 0


# ---------------- llm_keys（每用户私有 Key 池） ----------------

_KEY_COLS = "id,username,provider,model,base_url,api_key,label,sort_order,enabled,created_at,updated_at"


def _mask_key(key: str) -> str:
    """脱敏：sk-abcdefgh1234 → sk-***h1234"""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def add_llm_key(username: str, provider: str, api_key: str, model: Optional[str] = None,
                base_url: Optional[str] = None, label: str = "", sort_order: int = 0,
                db_path: Optional[str] = None) -> int:
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO llm_keys(username,provider,model,base_url,api_key,label,sort_order,"
            "enabled,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
            (username, provider, model, base_url, api_key, label, int(sort_order), _now()))
    return int(cur.lastrowid)


def list_llm_keys(username: str, db_path: Optional[str] = None) -> list[dict]:
    """属主自己的全部 key（明文，供调用层用）；接口层需另行脱敏"""
    with conn(db_path) as c:
        rows = c.execute(
            f"SELECT {_KEY_COLS} FROM llm_keys WHERE username=? ORDER BY sort_order, id",
            (username,)).fetchall()
    return [{"id": r[0], "username": r[1], "provider": r[2], "model": r[3], "base_url": r[4],
             "api_key": r[5], "label": r[6], "sort_order": r[7], "enabled": bool(r[8]),
             "created_at": r[9], "updated_at": r[10]} for r in rows]


def update_llm_key(key_id: int, username: str, db_path: Optional[str] = None,
                   **fields: Any) -> bool:
    """只允许属主修改自己的 key"""
    allowed = {"provider", "model", "base_url", "api_key", "label", "sort_order", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    sets = ",".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [_now(), key_id, username]
    with conn(db_path) as c:
        cur = c.execute(f"UPDATE llm_keys SET {sets},updated_at=? WHERE id=? AND username=?", vals)
    return cur.rowcount > 0


def delete_llm_key(key_id: int, username: str, db_path: Optional[str] = None) -> bool:
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM llm_keys WHERE id=? AND username=?", (key_id, username))
    return cur.rowcount > 0


def llm_keys_masked(username: str, db_path: Optional[str] = None) -> list[dict]:
    """接口展示用：脱敏 api_key"""
    out = []
    for k in list_llm_keys(username, db_path):
        k["api_key"] = _mask_key(k["api_key"])
        out.append(k)
    return out


# ---------------- tasks ----------------

def create_task(task_id: str, name: str, task_type: str, payload: Optional[dict] = None,
                db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO tasks(id,name,type,status,progress,message,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, name, task_type, "pending", 0, "", _now(), json.dumps(payload or {}, ensure_ascii=False)))


def update_task(task_id: str, db_path: Optional[str] = None, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(task_id)
    with conn(db_path) as c:
        c.execute(f"UPDATE tasks SET {','.join(cols)} WHERE id=?", vals)


def update_progress(task_id: str, progress: float, message: str = "",
                    db_path: Optional[str] = None) -> None:
    """子进程直接写进度（短事务，WAL 下安全）"""
    with conn(db_path) as c:
        c.execute("UPDATE tasks SET progress=?, message=?, status='running' WHERE id=?",
                  (float(progress), message, task_id))


def finish_task(task_id: str, status: str, error: Optional[str] = None,
                payload: Optional[dict] = None, db_path: Optional[str] = None) -> None:
    fields: dict[str, Any] = {"status": status, "progress": 100.0 if status == "success" else None,
                              "finished_at": _now()}
    if status == "success":
        fields["error"] = None
    if error is not None:
        fields["error"] = error
    fields = {k: v for k, v in fields.items() if v is not None or k == "error"}
    with conn(db_path) as c:
        # payload 与创建时合并（保留 strategy_id/period/config 等创建期字段）
        if payload is not None:
            row = c.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            old: dict = {}
            if row and row[0]:
                try:
                    old = json.loads(row[0])
                except json.JSONDecodeError:
                    old = {}
            old.update(payload)
            fields["payload"] = json.dumps(old, ensure_ascii=False)
        cols = [f"{k}=?" for k in fields]
        c.execute(f"UPDATE tasks SET {','.join(cols)} WHERE id=?", (*fields.values(), task_id))


def get_task(task_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with conn(db_path) as c:
        row = c.execute(
            "SELECT id,name,type,status,progress,message,error,created_at,finished_at,payload "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[9] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {"task_id": row[0], "name": row[1], "type": row[2], "status": row[3], "progress": row[4],
            "message": row[5], "error": row[6], "created_at": row[7], "finished_at": row[8],
            "payload": payload}


def list_tasks(task_type: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        if task_type:
            rows = c.execute(
                "SELECT id,name,type,status,progress,message,error,created_at,finished_at,payload "
                "FROM tasks WHERE type=? ORDER BY created_at DESC, rowid DESC", (task_type,)).fetchall()
        else:
            rows = c.execute(
                "SELECT id,name,type,status,progress,message,error,created_at,finished_at,payload "
                "FROM tasks ORDER BY created_at DESC, rowid DESC").fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row[9] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append({"task_id": row[0], "name": row[1], "type": row[2], "status": row[3],
                    "progress": row[4], "message": row[5], "error": row[6], "created_at": row[7],
                    "finished_at": row[8], "payload": payload})
    return out


# ---------------- reports ----------------

def save_report(task_id: str, path: str, db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO backtest_reports(task_id, path, created_at) VALUES(?,?,?)",
                  (task_id, path, _now()))


def get_report_path(task_id: str, db_path: Optional[str] = None) -> Optional[str]:
    with conn(db_path) as c:
        row = c.execute("SELECT path FROM backtest_reports WHERE task_id=?", (task_id,)).fetchone()
    return row[0] if row else None


# ---------------- ai_analyses ----------------

def save_analysis(task_id: str, backtest_id: str, profile: str, model: str, status: str,
                  content: Optional[str], tokens_used: Optional[int], elapsed: Optional[float],
                  error: Optional[str], db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO ai_analyses(task_id,backtest_id,profile,model,status,content,"
            "tokens_used,elapsed,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, backtest_id, profile, model, status, content, tokens_used, elapsed,
             error, _now()))


def list_analyses(backtest_id: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        if backtest_id:
            rows = c.execute(
                "SELECT task_id,backtest_id,profile,model,status,content,tokens_used,elapsed,"
                "error,created_at FROM ai_analyses WHERE backtest_id=? ORDER BY id DESC",
                (backtest_id,)).fetchall()
        else:
            rows = c.execute(
                "SELECT task_id,backtest_id,profile,model,status,content,tokens_used,elapsed,"
                "error,created_at FROM ai_analyses ORDER BY id DESC").fetchall()
    return [{"task_id": r[0], "backtest_id": r[1], "profile": r[2], "model": r[3], "status": r[4],
             "content": r[5], "tokens_used": r[6], "elapsed": r[7], "error": r[8],
             "created_at": r[9]} for r in rows]


# ---------------- llm_usage ----------------

def record_llm_usage(profile: str, model: str, prompt_tokens: int, completion_tokens: int,
                     elapsed: float, db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO llm_usage(profile,model,prompt_tokens,completion_tokens,elapsed,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (profile, model, int(prompt_tokens), int(completion_tokens), float(elapsed), _now()))


def llm_usage_stats(db_path: Optional[str] = None) -> dict:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT profile, SUM(prompt_tokens+completion_tokens), COUNT(*) "
            "FROM llm_usage GROUP BY profile").fetchall()
    by_profile = {r[0]: {"tokens": int(r[1] or 0), "calls": int(r[2] or 0)} for r in rows}
    return {"total_tokens": sum(v["tokens"] for v in by_profile.values()),
            "total_calls": sum(v["calls"] for v in by_profile.values()),
            "by_profile": by_profile}
