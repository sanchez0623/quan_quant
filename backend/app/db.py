# -*- coding: utf-8 -*-
"""SQLite 元数据层：users / tasks / backtest_reports / ai_analyses / llm_usage
/ llm_keys / backtest_templates
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
  timeout REAL,                     -- 请求超时秒（空=用全局默认 LLM_TIMEOUT/300）
  max_tokens INTEGER,               -- 单次输出最大 token（空=用全局默认 LLM_MAX_TOKENS/32768）
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_keys_user ON llm_keys(username, sort_order);
CREATE TABLE IF NOT EXISTS backtest_templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,           -- 属主：每人只看到自己的模板
  name TEXT NOT NULL,               -- 模板名
  config TEXT NOT NULL,             -- 回测配置 JSON（与 BacktestRequest 同构）
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_templates_user ON backtest_templates(username, updated_at);
CREATE TABLE IF NOT EXISTS experiments(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  base_config TEXT NOT NULL,
  cells TEXT NOT NULL,
  capitals TEXT NOT NULL,
  matrix TEXT DEFAULT 'clock',   -- clock=趋势×T 2x2 / t_mode=四机制竞争
  sub_task_ids TEXT NOT NULL,
  start_date TEXT,
  end_date TEXT,
  status TEXT DEFAULT 'running',
  progress REAL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS bs_usage(
  date TEXT PRIMARY KEY,        -- YYYY-MM-DD
  count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bs_blacklist(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ip TEXT,
  freeze_count INTEGER NOT NULL DEFAULT 0,  -- 本年累计被限制次数
  detected_at TEXT,
  release_at TEXT,              -- 预计自动解除时间（空=未知，等5分钟刷新）
  last_check TEXT
);
-- ---------------- 实盘信号机（LIVE_SIGNAL_SYSTEM） ----------------
CREATE TABLE IF NOT EXISTS sig_pool(
  id INTEGER PRIMARY KEY CHECK (id = 1),    -- 单行滚动状态
  pool_json TEXT DEFAULT '[]',              -- 当前池子 [{code,name}]
  as_of TEXT,                               -- 最近一次选股基准日（T-1）
  gate_state INTEGER DEFAULT 0,             -- 池级开关 0=开 1=停开仓
  health_history TEXT DEFAULT '[]',         -- [{day,health}] 最近30日（滞回状态机输入）
  idle_start TEXT,                          -- 空仓开始日（重选判定）
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sig_config(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cfg_json TEXT DEFAULT '{}',               -- 盘前流程参数（above_ma/rank_key/top_x/...）
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sig_signal_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                         -- 产生时间
  kind TEXT NOT NULL,                       -- premarket 盘前 / intraday 盘中
  code TEXT,                                -- 空=组合级消息（池子/对账）
  name TEXT DEFAULT '',
  stype TEXT NOT NULL,                      -- 开仓/加仓/减仓/止损/清仓/池子/预警/对账
  reason TEXT DEFAULT '',
  suggest_amount REAL,
  ref_price REAL,
  status TEXT DEFAULT '待执行',              -- 待执行/已成交/已忽略/已过期/信息
  extra_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sig_signal_created ON sig_signal_log(created_at DESC);
CREATE TABLE IF NOT EXISTS sig_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER,                        -- 关联信号（空=手动补录）
  code TEXT NOT NULL,
  side TEXT NOT NULL,                       -- buy/sell
  fill_price REAL NOT NULL,
  fill_volume INTEGER NOT NULL,
  fee REAL DEFAULT 0,
  fill_time TEXT,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sig_position(
  code TEXT PRIMARY KEY,
  name TEXT DEFAULT '',
  volume INTEGER NOT NULL,
  cost_price REAL NOT NULL,
  open_day TEXT,
  group_id INTEGER,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sig_t_debt(
  code TEXT PRIMARY KEY,
  sold INTEGER DEFAULT 0,
  bought INTEGER DEFAULT 0,
  sell_amt REAL DEFAULT 0,
  buy_amt REAL DEFAULT 0,
  open_day TEXT,
  deadline_day TEXT
);
CREATE TABLE IF NOT EXISTS sig_withdraw(
  month TEXT PRIMARY KEY,
  total REAL DEFAULT 0,
  t_profit REAL DEFAULT 0,
  topup REAL DEFAULT 0,
  shortfall REAL DEFAULT 0,
  recover REAL DEFAULT 0,
  log_json TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS sig_strategy_state(
  code TEXT PRIMARY KEY,
  st_json TEXT DEFAULT '{}',    -- SlotStepper 状态快照（opened/full/adds_done/...）
  last_bar TEXT,                -- 已喂入的最后完成 bar（YYYY-MM-DD HH:MM 游标去重）
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sig_meta(
  k TEXT PRIMARY KEY,           -- 盘中心跳/断流熔断标记等 KV
  v TEXT DEFAULT '',
  updated_at TEXT
);
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
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """轻量迁移：老库补列（幂等）"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(ai_analyses)")}
    if "suggestions" not in cols:
        c.execute("ALTER TABLE ai_analyses ADD COLUMN suggestions TEXT")  # AI结构化建议 JSON
    ecols = {r[1] for r in c.execute("PRAGMA table_info(experiments)")}
    if "matrix" not in ecols:
        c.execute("ALTER TABLE experiments ADD COLUMN matrix TEXT DEFAULT 'clock'")
    kcols = {r[1] for r in c.execute("PRAGMA table_info(llm_keys)")}
    if "timeout" not in kcols:
        c.execute("ALTER TABLE llm_keys ADD COLUMN timeout REAL")
    if "max_tokens" not in kcols:
        c.execute("ALTER TABLE llm_keys ADD COLUMN max_tokens INTEGER")


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
    """删除用户并级联删除其 LLM keys 与回测模板；禁止删除管理员账号"""
    if username == config.ADMIN_USERNAME:
        return False
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM users WHERE username=?", (username,))
        c.execute("DELETE FROM llm_keys WHERE username=?", (username,))
        c.execute("DELETE FROM backtest_templates WHERE username=?", (username,))
    return cur.rowcount > 0


# ---------------- llm_keys（每用户私有 Key 池） ----------------

_KEY_COLS = "id,username,provider,model,base_url,api_key,label,sort_order,enabled,timeout,max_tokens,created_at,updated_at"


def _mask_key(key: str) -> str:
    """脱敏：sk-abcdefgh1234 → sk-***h1234"""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def add_llm_key(username: str, provider: str, api_key: str, model: Optional[str] = None,
                base_url: Optional[str] = None, label: str = "", sort_order: int = 0,
                timeout: Optional[float] = None, max_tokens: Optional[int] = None,
                db_path: Optional[str] = None) -> int:
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO llm_keys(username,provider,model,base_url,api_key,label,sort_order,"
            "enabled,timeout,max_tokens,created_at) VALUES(?,?,?,?,?,?,?,1,?,?,?)",
            (username, provider, model, base_url, api_key, label, int(sort_order),
             timeout, max_tokens, _now()))
    return int(cur.lastrowid)


def list_llm_keys(username: str, db_path: Optional[str] = None) -> list[dict]:
    """属主自己的全部 key（明文，供调用层用）；接口层需另行脱敏"""
    with conn(db_path) as c:
        rows = c.execute(
            f"SELECT {_KEY_COLS} FROM llm_keys WHERE username=? ORDER BY sort_order, id",
            (username,)).fetchall()
    return [{"id": r[0], "username": r[1], "provider": r[2], "model": r[3], "base_url": r[4],
             "api_key": r[5], "label": r[6], "sort_order": r[7], "enabled": bool(r[8]),
             "timeout": r[9], "max_tokens": r[10], "created_at": r[11], "updated_at": r[12]}
            for r in rows]


def update_llm_key(key_id: int, username: str, db_path: Optional[str] = None,
                   **fields: Any) -> bool:
    """只允许属主修改自己的 key"""
    allowed = {"provider", "model", "base_url", "api_key", "label", "sort_order", "enabled",
               "timeout", "max_tokens"}
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


def reset_task(task_id: str, db_path: Optional[str] = None) -> None:
    """把任务重置为 pending 并清空进度/错误，用于断点续传重新提交"""
    with conn(db_path) as c:
        c.execute("UPDATE tasks SET status='pending', progress=0, message='', "
                  "error=NULL, finished_at=NULL WHERE id=?", (task_id,))


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


def delete_task(task_id: str, db_path: Optional[str] = None) -> bool:
    """删除任务及关联记录（报告映射、AI 分析）。返回该任务是否存在过。"""
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM backtest_reports WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM ai_analyses WHERE backtest_id=? OR task_id=?", (task_id, task_id))
    return cur.rowcount > 0


# ---------------- ai_analyses ----------------

def save_analysis(task_id: str, backtest_id: str, profile: str, model: str, status: str,
                  content: Optional[str], tokens_used: Optional[int], elapsed: Optional[float],
                  error: Optional[str], suggestions: Optional[dict] = None,
                  db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO ai_analyses(task_id,backtest_id,profile,model,status,content,"
            "tokens_used,elapsed,error,suggestions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, backtest_id, profile, model, status, content, tokens_used, elapsed,
             error, json.dumps(suggestions, ensure_ascii=False) if suggestions else None,
             _now()))


def list_analyses(backtest_id: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        if backtest_id:
            rows = c.execute(
                "SELECT task_id,backtest_id,profile,model,status,content,tokens_used,elapsed,"
                "error,suggestions,created_at FROM ai_analyses WHERE backtest_id=? ORDER BY id DESC",
                (backtest_id,)).fetchall()
        else:
            rows = c.execute(
                "SELECT task_id,backtest_id,profile,model,status,content,tokens_used,elapsed,"
                "error,suggestions,created_at FROM ai_analyses ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        try:
            suggestions = json.loads(r[9]) if r[9] else None
        except json.JSONDecodeError:
            suggestions = None
        out.append({"task_id": r[0], "backtest_id": r[1], "profile": r[2], "model": r[3],
                    "status": r[4], "content": r[5], "tokens_used": r[6], "elapsed": r[7],
                    "error": r[8], "suggestions": suggestions, "created_at": r[10]})
    return out


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


def clear_llm_usage(db_path: Optional[str] = None) -> None:
    """清空用量统计（如清除测试期产生的脏数据）"""
    with conn(db_path) as c:
        c.execute("DELETE FROM llm_usage")


# ---------------- backtest_templates（每用户私有配置模板） ----------------

def add_template(username: str, name: str, config: dict,
                 db_path: Optional[str] = None) -> int:
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO backtest_templates(username,name,config,created_at) VALUES(?,?,?,?)",
            (username, name, json.dumps(config, ensure_ascii=False), _now()))
    return int(cur.lastrowid)


def list_templates(username: str, db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT id,name,config,created_at,updated_at FROM backtest_templates "
            "WHERE username=? ORDER BY id DESC", (username,)).fetchall()
    out = []
    for r in rows:
        try:
            config = json.loads(r[2])
        except json.JSONDecodeError:
            config = {}
        out.append({"id": r[0], "name": r[1], "config": config,
                    "created_at": r[3], "updated_at": r[4]})
    return out


def delete_template(template_id: int, username: str,
                    db_path: Optional[str] = None) -> bool:
    """只允许属主删除自己的模板"""
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM backtest_templates WHERE id=? AND username=?",
                        (template_id, username))
    return cur.rowcount > 0


# ---------------- experiments（对比实验） ----------------

def create_experiment(exp_id: str, name: str, base_config: dict, cells: list,
                      capitals: list, sub_task_ids: list, start_date: str,
                      end_date: str, matrix: str = "clock",
                      db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO experiments(id,name,base_config,cells,capitals,matrix,sub_task_ids,"
            "start_date,end_date,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (exp_id, name, json.dumps(base_config, ensure_ascii=False),
             json.dumps(cells, ensure_ascii=False),
             json.dumps(capitals, ensure_ascii=False), matrix,
             json.dumps(sub_task_ids, ensure_ascii=False),
             start_date, end_date, "running", _now()))


def get_experiment(exp_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with conn(db_path) as c:
        row = c.execute(
            "SELECT id,name,base_config,cells,capitals,sub_task_ids,start_date,end_date,"
            "status,progress,error,created_at,finished_at,matrix FROM experiments WHERE id=?",
            (exp_id,)).fetchone()
    if not row:
        return None
    return {
        "experiment_id": row[0], "name": row[1],
        "base_config": _jload(row[2]) or {}, "cells": _jload(row[3]) or [],
        "capitals": _jload(row[4]) or [], "sub_task_ids": _jload(row[5]) or [],
        "start_date": row[6], "end_date": row[7], "status": row[8],
        "progress": row[9], "error": row[10], "created_at": row[11],
        "finished_at": row[12],
        "matrix": row[13] or "clock",
    }


def list_experiments(db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT id,name,cells,capitals,sub_task_ids,status,progress,error,created_at,"
            "finished_at,matrix FROM experiments ORDER BY created_at DESC, rowid DESC").fetchall()
    out = []
    for r in rows:
        out.append({
            "experiment_id": r[0], "name": r[1], "cells": _jload(r[2]) or [],
            "capitals": _jload(r[3]) or [], "sub_task_ids": _jload(r[4]) or [],
            "status": r[5], "progress": r[6], "error": r[7], "created_at": r[8],
            "finished_at": r[9],
            "matrix": r[10] or "clock",
        })
    return out


def update_experiment(exp_id: str, db_path: Optional[str] = None, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(exp_id)
    with conn(db_path) as c:
        c.execute(f"UPDATE experiments SET {','.join(cols)} WHERE id=?", vals)


def delete_experiment(exp_id: str, db_path: Optional[str] = None) -> bool:
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM experiments WHERE id=?", (exp_id,))
    return cur.rowcount > 0


def _jload(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


# ---------------- 实盘信号机（LIVE_SIGNAL_SYSTEM） ----------------

def get_live_config(db_path: Optional[str] = None) -> dict:
    """盘前流程参数（above_ma/rank_key/top_x/exit_need/initial_capital/...）"""
    with conn(db_path) as c:
        row = c.execute("SELECT cfg_json FROM sig_config WHERE id=1").fetchone()
    return _jload(row[0]) if row and row[0] else {}


def save_live_config(cfg: dict, db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO sig_config(id, cfg_json, updated_at) VALUES(1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cfg_json=excluded.cfg_json, "
            "updated_at=excluded.updated_at",
            (json.dumps(cfg, ensure_ascii=False), _now()))


def get_live_pool(db_path: Optional[str] = None) -> dict:
    """池子滚动状态（pool/as_of/gate_state/health_history/idle_start）"""
    with conn(db_path) as c:
        row = c.execute(
            "SELECT pool_json, as_of, gate_state, health_history, idle_start, updated_at "
            "FROM sig_pool WHERE id=1").fetchone()
    if not row:
        return {}
    return {"pool": _jload(row[0]) or [], "as_of": row[1], "gate_state": row[2] or 0,
            "health_history": _jload(row[3]) or [], "idle_start": row[4],
            "updated_at": row[5]}


def save_live_pool(pool: list, as_of: Optional[str], gate_state: int,
                   health_history: list, idle_start: Optional[str],
                   db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO sig_pool(id, pool_json, as_of, gate_state, health_history, "
            "idle_start, updated_at) VALUES(1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET pool_json=excluded.pool_json, "
            "as_of=excluded.as_of, gate_state=excluded.gate_state, "
            "health_history=excluded.health_history, idle_start=excluded.idle_start, "
            "updated_at=excluded.updated_at",
            (json.dumps(pool, ensure_ascii=False), as_of, int(gate_state),
             json.dumps(health_history, ensure_ascii=False), idle_start, _now()))


def add_live_signal(kind: str, stype: str, code: Optional[str], name: str,
                    reason: str, suggest_amount: Optional[float],
                    ref_price: Optional[float], status: str = "待执行",
                    extra: Optional[dict] = None,
                    db_path: Optional[str] = None) -> int:
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO sig_signal_log(ts, kind, code, name, stype, reason, "
            "suggest_amount, ref_price, status, extra_json, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), kind, code, name, stype, reason, suggest_amount,
             ref_price, status, json.dumps(extra or {}, ensure_ascii=False), _now()))
        return int(cur.lastrowid)


def list_live_signals(limit: int = 100, status: Optional[str] = None,
                      db_path: Optional[str] = None) -> list[dict]:
    q = ("SELECT id, ts, kind, code, name, stype, reason, suggest_amount, ref_price, "
         "status, extra_json, created_at FROM sig_signal_log")
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with conn(db_path) as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(zip(["id", "ts", "kind", "code", "name", "stype", "reason",
                      "suggest_amount", "ref_price", "status", "extra", "created_at"], r))
        d["extra"] = _jload(d.get("extra")) or {}
        out.append(d)
    return out


def set_live_signal_status(signal_id: int, status: str,
                           db_path: Optional[str] = None) -> bool:
    with conn(db_path) as c:
        cur = c.execute("UPDATE sig_signal_log SET status=? WHERE id=?",
                        (status, signal_id))
    return cur.rowcount > 0


def add_live_fill(signal_id: Optional[int], code: str, side: str,
                  fill_price: float, fill_volume: int, fee: float = 0.0,
                  fill_time: Optional[str] = None, note: str = "",
                  db_path: Optional[str] = None) -> int:
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO sig_fills(signal_id, code, side, fill_price, fill_volume, "
            "fee, fill_time, note, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signal_id, code, side, fill_price, fill_volume, fee, fill_time,
             note, _now()))
        return int(cur.lastrowid)


def list_live_fills(limit: int = 200, db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT id, signal_id, code, side, fill_price, fill_volume, fee, "
            "fill_time, note, created_at FROM sig_fills "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(zip(["id", "signal_id", "code", "side", "fill_price",
                      "fill_volume", "fee", "fill_time", "note", "created_at"], r))
            for r in rows]


def upsert_live_position(code: str, name: str, volume: int, cost_price: float,
                         open_day: Optional[str] = None,
                         group_id: Optional[int] = None,
                         db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO sig_position(code, name, volume, cost_price, open_day, "
            "group_id, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
            "volume=excluded.volume, cost_price=excluded.cost_price, "
            "open_day=excluded.open_day, group_id=excluded.group_id, "
            "updated_at=excluded.updated_at",
            (code, name, volume, cost_price, open_day, group_id, _now()))


def list_live_positions(db_path: Optional[str] = None) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT code, name, volume, cost_price, open_day, group_id, updated_at "
            "FROM sig_position ORDER BY code").fetchall()
    return [dict(zip(["code", "name", "volume", "cost_price", "open_day",
                      "group_id", "updated_at"], r)) for r in rows]


def remove_live_position(code: str, db_path: Optional[str] = None) -> bool:
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM sig_position WHERE code=?", (code,))
    return cur.rowcount > 0


def get_strategy_states(db_path: Optional[str] = None) -> dict[str, dict]:
    """盘中状态机快照：{code: {st: {...}, last_bar: 'YYYY-MM-DD HH:MM'}}"""
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT code, st_json, last_bar FROM sig_strategy_state").fetchall()
    return {r[0]: {"st": _jload(r[1]) or {}, "last_bar": r[2]} for r in rows}


def save_strategy_state(code: str, st: dict, last_bar: Optional[str],
                        db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO sig_strategy_state(code, st_json, last_bar, updated_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET "
            "st_json=excluded.st_json, last_bar=excluded.last_bar, "
            "updated_at=excluded.updated_at",
            (code, json.dumps(st, ensure_ascii=False), last_bar, _now()))


def get_meta(key: str, db_path: Optional[str] = None) -> Optional[str]:
    with conn(db_path) as c:
        row = c.execute("SELECT v FROM sig_meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str, db_path: Optional[str] = None) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO sig_meta(k, v, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, value, _now()))


def reset_live_data(keep_config: bool = True,
                    db_path: Optional[str] = None) -> None:
    """清空实盘信号机数据（信号流水/回填/虚拟持仓/池子状态/做T债务/出金/
    盘中状态机快照/KV）。keep_config=True 保留 sig_config（流程参数配置）。
    表名来自白名单常量。"""
    tables = ["sig_signal_log", "sig_fills", "sig_position", "sig_pool",
              "sig_t_debt", "sig_withdraw", "sig_strategy_state", "sig_meta"]
    if not keep_config:
        tables.append("sig_config")
    with conn(db_path) as c:
        for t in tables:
            c.execute(f"DELETE FROM {t}")
