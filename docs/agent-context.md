# Agent 项目上下文地图

> 给 AI 编程 agent 的架构速查。详细规则见根目录 `AGENTS.md`。

## 技术栈

- **后端**：Python 3.11 / FastAPI / polars / SQLite / Optuna / ProcessPoolExecutor
- **前端**：React 18 / TypeScript / Vite / Ant Design 5 / KLineCharts 9 / ECharts
- **部署**：Docker Compose（单容器 app + Caddy 反代自动 HTTPS）
- **存储**：Parquet 数据湖（行情）+ `data/meta.db`（SQLite 业务元数据）

## 核心流程

```
用户提交回测 → api/backtests.py（normalize_config 校验+落库）
  → task_manager.py（ProcessPoolExecutor 分发）
    → engine/（事件驱动回测：datafeed 加载 → 策略产生信号 → broker 撮合）
      → 输出 JSON 报告 → db.save_report() → 前端 WebSocket 推进度

用户提交寻优 → api/optimize.py
  → optimizer.py（Optuna 贝叶斯 + MedianPruner + SQLite 断点续跑）
    → 内部循环调用回测引擎
```

## 关键文件索引

| 文件 | 职责 |
|---|---|
| `backend/app/main.py` | FastAPI 入口，路由注册 + WebSocket |
| `backend/app/config.py` | 环境变量、路径常量（含 `META_DB_PATH`） |
| `backend/app/db.py` | SQLite CRUD（tasks / reports / users / keys） |
| `backend/app/task_manager.py` | 进程池任务调度与进度推送 |
| `backend/app/logging_setup.py` | 日志体系：`setup()` 进程级、`task_context(task_id)` 任务归档 |
| `backend/app/api/backtests.py` | 回测任务 CRUD + `normalize_config`（顶层字段登记表） |
| `backend/app/engine/broker.py` | 撮合模拟（T+1 / 涨跌停 / 滑点 / 手续费） |
| `backend/app/engine/datafeed.py` | 行情数据加载（polars 向量化） |
| `backend/app/engine/strategies/` | 策略实现（momentum_t / dragon_dip 等） |
| `backend/app/data/store.py` | Parquet 读写与数据湖管理 |
| `backend/app/data/sources.py` | 多数据源抽象（baostock/akshare/mootdx） |
| `backend/app/data/updater.py` | 每日定时增量更新（ENABLE_SCHEDULER=1 时启动） |
| `backend/app/llm/` | LLM 多 Provider fallback chain |
| `frontend/src/pages/backtest/` | 回测主页面（表单 + 列表 + 结果图表） |
| `frontend/src/components/` | 通用组件（K线图 / 参数表单 / 风控表单等） |

## 数据库约定

- **业务库**：`data/meta.db`（`config.META_DB_PATH`），含 tasks / reports / users / llm_keys 表
- **`backend/app.db`**：遗留/测试用，与业务无关，勿混淆
- **行情数据**：`data/` 下 Parquet 文件，由 `backend/app/data/store.py` 统一读写

## API 契约

所有 API 变更必须同步更新 `docs/API_CONTRACT.md`。

## 日志约定

- 业务模块统一 `logging.getLogger(__name__)`；禁止新增静默 `except Exception: pass`（记录 + 降级）
- 任务链路自动归档 `data/logs/task/<task_id>.log`（JSON Lines，恒 DEBUG 全量）；进程级 `data/logs/app-YYYYMMDD.log`
- 级别：`LOG_LEVEL`（默认 INFO）控控制台与日归档；保留期 `LOG_RETENTION_DAYS`（默认 30 天）

## 常用命令

见根目录 `AGENTS.md`「常用命令」一节。
