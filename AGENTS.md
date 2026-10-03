# 项目协作规则（Codex / Claude Code / WorkBuddy / Trae 通用）

> 本文件是所有 AI 编程 agent 的单一事实源。各 agent 专属文件（CLAUDE.md、.trae/rules/）均指向此处，规则改动只改本文件。

## 沟通与输出格式

- **改动需用户明示（2026-09-03 用户明确要求）**：用户询问"要不要修 / 怎么做 / 需要吗"这类问题时，**只给结论与理由，不得直接改代码**——包括"顺手优化""帮用户修掉"这类自作主张。只有用户明确说"修/改/实现/继续"后才动手。曾因擅自压制一条第三方警告被用户纠正（"我让你拦截了吗。。不要自作主张"）。

- **K线拉取需用户明示许可（2026-09-28 用户明确要求）**：任何任务未经用户允许，**不得直接拉取K线数据**（日线/分钟线/任意周期，含补洞、回填、增量更新、任何数据源 baostock/mootdx/akshare 等）；本地已有数据只读不受限。**数据不够时，先汇报缺口内容、规模与可选方案，交用户决策后再动手**，不得自行启动任何拉取任务。

- **K线拉取并发上限 ≤3 线程（2026-09-28 用户明确要求，baostock 6小时封禁事故）**：即使经用户允许去拉K线，**并发/线程数也不得超过 3**（数据源按频率封 IP，baostock 冻结 6 小时/次）；拉取前先查 `bs_usage.get_monitor()` 黑名单状态，拉取中控制调用频率。

- **方案取舍不得擅自决定（2026-09-04 用户明确要求）**：涉及策略走向、默认值变更、方案推荐/设默认、实验设计（跑哪些 A/B、参数怎么调、改哪个方向）、是否需要重启服务等**判断性决策**，一律先给出**结论 + 理由 + 候选选项**，等用户明确拍板后再执行；执行"实现方案 X"这类指令时，只做用户点名的范围，**不得顺手扩大**（如用户说"接入 B 和 D"，就只做 B 和 D 的实现与验证，不擅自给 B 定"推荐值/设默认"，不擅自扩展实验矩阵或推进 A/C）。曾因自行判定"A/B 证伪就回退代码、把 B 参数定为推荐、倾向继续做 A/C"被用户纠正（"不要自己乱下决定，写入规则"）。

- **参数中英对照**：对话与文档中提到回测/寻优参数时，英文 key 后必须附中文标签（首次出现时），如 `mom_short（短周期动量）`、`exit_need（衰退信号满足数）`。中文标签以 `param_schema[].label` 与 RiskConfigForm 的字段名为准。

- 性能/待办类结论记录到 `docs/PERF_TODO.md`、调优硬编码记录到 `docs/TUNING_TODOS.md`；API 变更必须同步 `docs/API_CONTRACT.md`。

- **日志与静默异常（2026-10-03 起）**：业务模块统一 `logging.getLogger(__name__)`，**不再新增 `except Exception: pass`**——一律「记录 + 降级」：影响结果的降级用 `logger.warning(..., exc_info=True)`，可预期的高频失败（多源探测、可选 AI 增强项）用 `logger.debug(..., exc_info=True)`。任务链路日志自动归档到 `data/logs/task/<task_id>.log`（JSON Lines，恒为 DEBUG 全量），进程级归档 `data/logs/app-YYYYMMDD.log`（级别由 `LOG_LEVEL` 控制）；排障先看 task 归档。

- 完成代码改动后默认 commit + push（用户明确要求过的工作流）。

## 常用命令

| 操作 | 命令 |
|---|---|
| 后端测试 | `cd backend; python -m pytest tests/ -q` |
| 前端类型检查 | `cd frontend; npx tsc --noEmit` |
| 后端静态检查 | `python -m ruff check .`（仓库根目录，配置见 `ruff.toml`） |
| 后端启动（开发） | `cd backend; ..\.venv\Scripts\python.exe run.py` |
| 前端启动（开发） | `cd frontend; npm run dev` |
| Docker 启动 | `docker compose up -d --build` |

## 项目结构速查

```
backend/
  app/
    main.py            # FastAPI 入口，挂载所有路由 + WebSocket
    config.py          # 环境变量与路径配置
    db.py              # SQLite 元数据库（data/meta.db）
    auth.py            # JWT 认证
    task_manager.py    # ProcessPoolExecutor 任务调度
    optimizer.py       # Optuna 参数寻优
    api/               # REST 路由（backtests/strategies/optimize/ai/keys/users…）
    engine/            # 自研回测引擎
      broker.py        #   撮合（T+1、涨跌停、滑点、手续费）
      datafeed.py      #   行情加载（polars）
      strategies/      #   策略实现（momentum_t / dragon_dip …）
    data/              # 数据层（Parquet 数据湖 + SQLite 元数据 + 多源抽象）
      store.py         #   Parquet 读写
      sources.py       #   baostock/akshare/mootdx 数据源
      updater.py       #   每日定时增量更新
      meta.db          # ⚠️ 业务库在 data/meta.db，不是 backend/app.db
    live/              # 实盘信号（非下单）
    llm/               # LLM 多 Provider + fallback chain
  tests/               # pytest

frontend/
  src/
    api/               # axios 封装
    pages/             # 页面组件（backtest/ 主回测页）
    components/        # 通用组件
    layouts/           # 布局
    context/           # React Context
    hooks/             # 自定义 hooks
    utils/             # 工具函数

config.example/        # 配置模板（入库）
config/                # 实际配置（不入库，gitignore）
data/                  # Parquet 数据湖（不入库）
docs/                  # 设计文档与 API 契约
scripts/               # 辅助脚本（experiments/ 收敛一次性 _*.py，不参与 CI）
```

## 关键业务约定（易错点）

- **实验回测必须落库进回测列表（2026-09-13 用户明确要求）**：任何实验性回测产出（OAT/AB/采纳形态/对照形态）只要结论要给用户复查，就必须用三件套落库：`db.create_task(tid, name, "backtest", payload)` + `db.save_report(tid, path)` + `db.update_task(tid, status="success", progress=100)`（payload 含 config 与 report_path，参照 `scripts/save_pulse_tasks.py`）。只写 reports/*.json 不登记 tasks 表 = 界面完全不可见（历史事故两次）。业务库在 `data/meta.db`（`config.META_DB_PATH`），与 `backend/app.db` 无关。

- **重点/有效任务打标签（2026-09-13 用户要求；2026-09-15 升级为独立字段）**：回测实验中"重点有效"（采纳形态、关键对照、结论载体）的任务，落库时**打 `tag` 字段**（`db.create_task(..., tag="重点")` 或补打 `db.update_task(tid, tag="重点")`）——标签是 tasks.tag 独立列，**名称不再加 🏷️ 前缀**（历史前缀已迁移：名称去 🏷️、tag='重点'）。前端列表有"标签"列与筛选下拉（全部标签/重点/无标签），`GET /api/backtests?tag=重点` 服务端精确筛选（`tag=__none__`=只看无标签）。

- 无后视镜：选股/重选基准日 = 严格早于段首的最近交易日（T-1），任何新选股逻辑不得引入未来数据。

- 动态选股（universe_auto）仅支持 momentum_t / momentum_slot；寻优模板必须是静态池（前端会自动固化动态池）。

- 寻优防过拟合三件套默认开启：多窗口（n_windows≥3）、跨窗方差惩罚（λ≥0.5）、回撤熔断线（dd_floor）。

- momentum_slot 的 pool_n（候选池大小）必须 > max_holdings（最大持仓只数），否则轮动机制失效。

- max_holdings（最大持仓只数）有**两处**需保持一致：策略参数"核心开关"组（策略层槽位管理）与风控配置（引擎最终屏障）；实际约束取更严格者，两处填不同值无意义。

- **回测顶层字段单一事实源（代码约束，取代旧「4 处同步」流程约束）**：回测表单可调顶层字段的「键 / 标签 / 类型 / 默认值 / 选项 / 数值范围」统一写在 `backend/app/api/backtest_schema.py` 的 `TOP_FIELDS`——后端 `BacktestRequest` 由它 `create_model` 生成、`normalize_config` 的默认值由 `top_level_defaults()` 派生、前端通过 `GET /api/backtests/meta` 获取（`initialValues` / `buildConfigFromValues` / `applyConfigToForm` 均从接口取值，不再手抄）。**新增顶层字段只改这一处**；`backend/tests/test_backtest_form_contract.py` 在 CI 上兜底：① 模型字段 ↔ schema 键集合必须一致（拦「pydantic 静默丢弃」）；② 默认值必须与 schema 一致；③ 每个可渲染字段必须在前端表单出现（拦「新增了字段但界面填不到」）。历史事故：`auto_rank_key` / `nav_take_profit_pct` 未登记导致模板落库缺失；`pool_refill_min` 未进模型被 pydantic 静默丢弃、后端兼底 2，而前端/引擎/文档都是 0=关闭（用户手动建的「采纳形态复现」任务被静默配成已证伪组合）。参数中英对照以 schema 的 `label` 为准。

- 池级趋势开关（pool_gate）与 universe_auto 正交互补：gate 管"能不能买"，重选管"买谁"；换池时 gate 随新池重置（新池=门槛筛选产物，无需确认期）。

- SQLite executescript 中 SQL 注释只能用 `--`，不能用 `#`。
