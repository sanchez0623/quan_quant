# -*- coding: utf-8 -*-
"""FastAPI 入口：CORS、路由挂载、WebSocket 进度推送、启动初始化、静态托管"""
import asyncio
import contextlib
import threading
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

# 精准压制第三方依赖的启动噪音（py-mini-racer 0.6.0 内部 import pkg_resources，
# setuptools<81 仍可用；升级该库可能破坏 akshare 兼容，故只压警告不升依赖）
warnings.filterwarnings("ignore", message="pkg_resources is deprecated",
                        category=UserWarning, module="py_mini_racer")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, config, db
from .api import (ai, auth as api_auth, backtests, data as api_data, experiments, keys,
                  live, optimize, stocks, strategies, users)
from .task_manager import manager

FINAL_STATES = ("success", "failed", "cancelled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化 DB、确保 admin 存在、启动 TaskManager（惰性进程池）
    config.ensure_dirs()
    db.init_db()
    if db.get_user(config.ADMIN_USERNAME) is None:
        db.create_user(config.ADMIN_USERNAME, auth.hash_password(config.ADMIN_PASSWORD))

    # 启动：后台预热数据源健康检查（不阻塞启动；数据管理页可立即显示源可用性）
    def _warmup_health():
        try:
            from .data import sources
            sources.check_health(timeout=8)
        except Exception:
            pass
    threading.Thread(target=_warmup_health, daemon=True, name="health-warmup").start()

    scheduler = None
    if config.ENABLE_SCHEDULER:
        # 每日 16:10 数据更新（默认 disabled，ENABLE_SCHEDULER=1 启用）
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        def _daily_update():
            import uuid
            from . import db as _db
            task_id = "data_" + uuid.uuid4().hex[:12]
            _db.create_task(task_id, "定时数据更新", "data_update", payload={"scope": "daily"})
            manager.submit("data_update", task_id, scope="daily")

        scheduler = BackgroundScheduler()
        scheduler.add_job(_daily_update, CronTrigger(hour=16, minute=10))
        scheduler.start()
    yield
    # 关闭
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    manager.shutdown()


app = FastAPI(title="A股个人量化回测系统", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5177", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# P1-2：响应 GZip 压缩——回测报告 JSON（含数万条交易日志）传输体积降约 90%，
# 前端详情页打开显著提速；浏览器 fetch 自动解压，前端零改动
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(api_auth.router)
app.include_router(strategies.router)
app.include_router(stocks.router)
app.include_router(backtests.router)
app.include_router(optimize.router)
app.include_router(experiments.router)
app.include_router(ai.router)
app.include_router(api_data.router)
app.include_router(live.router)
app.include_router(keys.router)
app.include_router(users.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(ws: WebSocket, task_id: str):
    """每 0.5s 轮询 SQLite，有变化才推送；终态推最后一条后关闭"""
    await ws.accept()
    last = None
    try:
        while True:
            task = await asyncio.get_running_loop().run_in_executor(
                None, lambda: db.get_task(task_id))
            if task is None:
                await ws.send_json({"status": "failed", "progress": 0,
                                    "message": "", "error": "任务不存在"})
                break
            snap = {"status": task["status"],
                    "progress": round(task["progress"] or 0, 1),
                    "message": task.get("message") or ""}
            if snap != last:
                if task.get("error"):
                    snap["error"] = task["error"]
                await ws.send_json(snap)
                last = snap
            if task["status"] in FINAL_STATES:
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


# 静态托管（frontend/dist 存在时）
_dist = Path(config.PROJECT_ROOT) / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
