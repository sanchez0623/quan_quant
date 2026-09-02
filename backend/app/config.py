# -*- coding: utf-8 -*-
"""全局配置：环境变量 + .env 加载 + 路径常量"""
import os
import secrets
import sys
from pathlib import Path

# 项目根 = backend/ 的上级目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """手写 .env 解析（不引入 python-dotenv），文件不存在时忽略"""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# JWT 密钥：未配置则随机生成（每次重启失效，打印警告）
JWT_SECRET = _get("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    print("[config] 警告: 未设置 JWT_SECRET，已随机生成（重启后旧 token 失效）。"
          "请在 .env 中配置固定值。", file=sys.stderr)

ADMIN_USERNAME = _get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _get("ADMIN_PASSWORD", "admin123")

# 目录
DATA_DIR = Path(_get("DATA_DIR", str(PROJECT_ROOT / "data")))
CONFIG_DIR = Path(_get("CONFIG_DIR", str(PROJECT_ROOT / "config")))

# 合成数据起始（默认 5 年前）
DATA_START_DATE = _get("DATA_START_DATE", "")

# 调度器开关（默认禁用）
ENABLE_SCHEDULER = _get("ENABLE_SCHEDULER", "0") not in ("0", "false", "False")

# 寻优 trial 并行度（默认 1=串行批处理）。设为 2/3 时每组 trial 分给多个
# 子进程波次并行执行；单 trial 内存峰值可达数 GB，请保证
# 并行度 × 单trial峰值 < 可用物理内存，否则会被系统杀进程。
OPTIMIZE_PARALLEL_TRIALS = max(1, int(_get("OPTIMIZE_PARALLEL_TRIALS", "1") or 1))

# 派生路径
META_DB_PATH = DATA_DIR / "meta.db"
REPORTS_DIR = DATA_DIR / "reports"
OPTUNA_DIR = DATA_DIR / "optuna"
MINUTE5_DIR = DATA_DIR / "minute5"

JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_SECONDS = 86400

# LLM 各 provider 的 api_key 环境变量（llm.yaml 中 api_key_env 指向的名字在此查找）
LLM_KEY_ENVS = [
    "SILICONFLOW_API_KEY",
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY",
    "OLLAMA_API_KEY",
]

# 飞书机器人 webhook（实盘信号推送，LIVE_SIGNAL_SYSTEM §6；.env 中配置，严禁入库）
FEISHU_WEBHOOK_URL = _get("FEISHU_WEBHOOK_URL", "")


def ensure_dirs() -> None:
    """确保运行期目录存在"""
    for p in (DATA_DIR, REPORTS_DIR, OPTUNA_DIR, MINUTE5_DIR, CONFIG_DIR):
        p.mkdir(parents=True, exist_ok=True)
