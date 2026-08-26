# A股个人量化回测系统

面向个人及少量朋友使用的 A 股量化回测平台（不接真实券商、不做实盘下单）。支持日线与 5 分钟线策略回测、加减仓与做 T（底仓当日回转）、分层持仓、参数化风控、完善绩效统计，并接入 AI 大模型进行评估与调优。

完整设计方案见《A股个人量化回测系统-完整实现方案.docx》。

## 功能特性

- **自研事件驱动回测引擎**：向量化信号（polars）+ 逐 bar 撮合，精确模拟 A 股 T+1、涨跌停（一字板不成交）、滑点、手续费（佣金/印花税/过户费）
- **分层持仓账户**：每笔开仓独立 Position，天然支持金字塔加仓、分批止盈；做 T 采用"卖旧买新"模型，做 T 贡献独立核算
- **双周期回测**：日线 + 5 分钟线（分钟级涨跌停按前一交易日收盘价判定）
- **参数化风控**（与策略参数分离）：个股/总仓位上限、固定/ATR/移动止损、止盈、最大回撤熔断、日内交易次数限制
- **完善统计**：年化收益、最大回撤、夏普/索提诺/卡玛、胜率、盈亏比、做 T 与加减仓贡献分解、月度收益热力图
- **Optuna 参数寻优**：贝叶斯优化 + MedianPruner 剪枝 + SQLite 存储断点续跑；样本内/外 70/30 划分 + 过拟合风险评级
- **多用户 + 私有 Key 池**：admin 创建账号；每位用户在「Key 管理」页维护自己的 LLM API Key（存数据库，前端增删改），支持 DeepSeek / OpenRouter / 火山方舟 / 智谱 / 硅基流动 / Ollama / 任意自定义 OpenAI 兼容端点；一个 key 余额不足/失效/限流自动跨服务商无缝切换下一个；回测、寻优、数据等功能全体用户共享
- **AI 辅助调优**：回测报告解读与优化建议（用发起人自己的 Key 池），token 用量统计
- **数据层**：Parquet 数据湖 + SQLite 元数据；多数据源抽象（baostock/akshare/mootdx，可选安装）健康检查与自动降级；内置合成演示数据一键生成
- **前后端分离**：React + AntD + KLineCharts（K 线买卖点标记）+ ECharts；WebSocket 任务进度推送

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + Ant Design 5 + KLineCharts 9 + ECharts |
| 后端 | FastAPI + Uvicorn + WebSocket |
| 数据处理 | polars + pandas + PyArrow |
| 存储 | Parquet（行情）+ SQLite（元数据） |
| 任务执行 | ProcessPoolExecutor 进程池（≤3 并发） |
| 参数寻优 | Optuna |
| AI | OpenAI 兼容协议多 Provider + fallback chain |
| 部署 | Docker Compose + Caddy（自动 HTTPS） |

## 快速开始（本地开发）

### 1. 准备环境

- Python 3.11+，Node.js 18+

推荐使用 venv 虚拟环境隔离 Python 依赖（避免污染全局、多项目互不干扰）：

```bash
# 后端依赖（venv 虚拟环境）
python -m venv .venv
.venv\Scripts\pip install -r backend/requirements.txt        # Windows
# .venv/bin/pip install -r backend/requirements.txt          # macOS / Linux

# 真实数据源（可选，不装则用演示数据）
# 注意：必须用 venv 的 pip（如 .venv\Scripts\pip），裸 pip 会装进全局 Python
.venv\Scripts\pip install -r backend/requirements-sources.txt   # Windows
# .venv/bin/pip install -r backend/requirements-sources.txt     # macOS / Linux
# 提示：mootdx 固定依赖 httpx<0.26，安装时 pip 会把 httpx 降级并警告与
# requirements.txt 的 httpx>=0.27 冲突——属预期现象，项目代码兼容 0.25.x

# 前端依赖
cd frontend
npm install
```

### 2. 配置密钥

```bash
cp .env.example .env
# 编辑 .env：设置 JWT_SECRET（openssl rand -hex 32）与 ADMIN_PASSWORD
```

**LLM Key 配置（推荐，前端管理）**：登录后进入「Key 管理」页添加你的 API Key——支持 DeepSeek / OpenRouter / 火山方舟 / 智谱 / 硅基流动 / Ollama 及任意自定义 OpenAI 兼容端点。Key 存本地 SQLite（data/meta.db，不入 Git），仅自己可见。配多个 Key 时按优先级自动轮换：某个 Key 余额不足(402)/失效(401)/限流(429)时**跨服务商无缝切换**下一个。

系统级兜底（可选）：`.env` 中配置 `LLM_KEY_1~9`（格式 `provider|key` 或 `provider|model|key`）作为所有用户共用的公共池；用户无私有 Key 时自动使用。

**多用户**：admin 登录后在「用户管理」页为朋友创建账号。每位用户管理自己的 Key；回测、寻优、AI 分析结果、数据等所有功能全体用户共享。

### 3. 启动

```bash
# 终端 1：后端（http://localhost:8000）
cd backend
..\.venv\Scripts\python.exe run.py      # Windows（venv）

# 终端 2：前端（http://localhost:5177，已代理 /api 与 /ws）
cd frontend && npm run dev
```

### 4. 生成演示数据并跑第一个回测

无真实数据源时，登录后进入"数据管理"页 → 点击"生成演示数据"（合成 5 只股票约 2 年日线+5 分钟线），然后到"回测中心"新建回测：
- 策略选"双均线策略"（日线）或"网格做T策略"（5 分钟，体验做 T 与贡献分解）
- 股票池搜索代码添加（如 600000、000001）
- 时间区间取数据实际范围 → 提交，进度条完成后查看 K 线买卖标记、资金曲线、统计报告

默认账号 `admin / admin123`（生产环境务必通过 .env 修改）。

### 5. 真实数据（可选）

安装可选数据源依赖后，"数据管理"页可用"增量更新"拉取真实行情（日线主源 baostock，分钟线主源 mootdx，自动降级）。

## 测试

```bash
cd backend
python -m pytest tests/ -q        # 50 项引擎/API 单元测试
cd ..
python scripts/e2e_check.py      # 端到端联调检查（需后端已启动，34 项）
```

## Docker 部署（阿里云等）

```bash
cp .env.example .env       # 填写密钥
cp -r config.example config  # 按需修改 LLM 配置
# 修改 Caddyfile 域名后：
docker compose up -d --build
```

- 单容器跑 FastAPI + 前端静态文件（frontend/dist 由镜像内多阶段构建产出）
- Caddy 反代并自动申请/续期 HTTPS 证书
- 数据（data/）、配置（config/）、密钥（.env）全部 volume/env 注入，容器无状态可随时重建
- 安全：仅开放 80/443；SSH 改密钥登录；数据库不对外暴露

## 安全须知（GitHub 开源）

- **严禁提交** `.env`、`config/`、`data/`（已在 .gitignore 排除）
- LLM 配置只存环境变量名（`api_key_env`），密钥值不出现在任何入库文件
- 提交前建议启用 gitleaks 扫描：`pip install pre-commit && pre-commit install`
- 若曾误提交密钥：用 git filter-repo/BFG 清洗历史并**立即作废该密钥**

## 项目结构

```
├── backend/               # FastAPI 后端
│   ├── app/
│   │   ├── engine/        # 回测引擎（broker/portfolio/risk/stats/runner/strategies）
│   │   ├── data/          # Parquet 存储、数据源适配、更新服务、合成数据
│   │   ├── llm/           # 多 LLM Provider + 报告分析
│   │   ├── api/           # REST 路由（auth/backtests/optimize/ai/data...）
│   │   ├── optimizer.py   # Optuna 寻优封装
│   │   └── task_manager.py# 进程池任务系统 + 进度上报
│   └── tests/             # 单元测试
├── frontend/              # React 前端（src/pages 各功能页）
├── docs/                  # API 契约与前后端开发规范
├── scripts/               # 端到端联调脚本
├── config.example/        # LLM 配置模板（入库）
├── Dockerfile / docker-compose.yml / Caddyfile
└── .env.example           # 环境变量模板（入库）
```

## 内置策略

| 策略 | 周期 | 说明 |
|---|---|---|
| ma_cross 双均线 | 日线/5分钟 | 快线上穿慢线买入、下穿卖出，支持加仓 |
| grid_t 网格做T | 5分钟（推荐） | 底仓 + ATR 自适应网格（动态阈值=近N日ATR/close×倍数），高抛低吸做 T |
| momentum_t 动量趋势+做T | 5分钟 | 动量为主+做T增强：MACD三重确认建仓（底仓10%~70%动态）、横截面动量排名选股、金字塔加仓、过热减仓、双确认清仓；ATR分位自适应非对称网格做T（动态T比例+费用下限） |

策略通过 `param_schema` 声明参数，前端动态渲染表单；新策略实现 `Strategy.prepare()` 并注册即可。

## 引擎与账户能力

- **指标预热**：策略声明 `warmup_days`，引擎自动前推数据加载窗口（指标就绪前不出信号、不交易），避免回测起点附近信号失真
- **月度出金（落袋为安）**：`monthly_withdraw_base`（月度目标额，0=关闭）+ `t_profit_withdraw_pct`（逐笔T盈利即时提成%）。每笔做T盈利即时提取 x%；月末不足目标额自动补齐；护栏保证累计提取不吃本金，现金不足记缺口。所有绩效指标基于**调整净值**（真实净值+累计提取），出金不算亏损；报告新增 `withdrawal_coverage`（月度足额提取占比）
- **持仓规划**：`max_holdings`（最大持仓只数，只限制新开仓）+ `cash_reserve_pct`（现金缓冲，永不进场的资金，用于出金兜底）
- **完整生命周期订单**：开仓（动态预算）/ 金字塔加仓（递减预算）/ 按比例减仓（允许零股卖出）/ 做T（动态比例+最小金额保护）/ 清仓 / 止损止盈
- **交易成本**：佣金（双边，最低5元）+ 印花税（卖出）+ 经手费 + 证管费 + 过户费，全部可配置（默认按现行官方费率）

## 已知限制

- 单次回测不做 bar 级断点续跑（重跑代价小；寻优由 Optuna 原生支持断点续跑）
- 数据源 baostock/akshare/mootdx 为可选依赖，接口变更可能需要适配
- AI 分析需至少配置一个 LLM API Key，未配置时任务友好失败并提示
