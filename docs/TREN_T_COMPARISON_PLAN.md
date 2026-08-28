# 趋势×做T 对比实验方案（TREN_T_COMPARISON）

> 状态：方案设计（待实现）
> 日期：2026-08-27
> 目的：用 2×2 因子实验回答两个悬而未决的问题——**T 层到底有没有价值？趋势层该用日线时钟还是盘中时钟？**
> 关联文档：`MOMENTUM_T_AUDIT.md`（审计）、`OPTIMIZE_AND_AI_PLAN.md`（寻优/AI 方案）

---

## 1. 前置条件状态核实（2026-08-27 代码实查）

| # | 事项 | 状态 | 核实依据 |
|---|---|---|---|
| P0-1 | A1 日线特征 T-1 化 | ✅ **已修复** | `momentum_t.py` prepare()：`feats.shift(-1)` 后移一交易日再 join，当日 bar 只见 T-1 特征 |
| P0-2 | B7-② 网格重建底仓限额 | ✅ **已修复** | `momentum_t.py` L397-399：网格买点信号携带 `budget_pct = base_pct_min`（试仓档）；`runner.py` execute_buy L304-306：空仓重建时 `budget = min(完整风控预算, equity × base_pct_min%)` 封顶；做T债务买回路径不读 budget_pct 不受影响。效果：600309 式"95% 权益重建"变为最多 10% 权益试仓 |
| P0-2b | B7-① 崩溃保护绝对上限 | ✅ **已修复** | `momentum_t.py` 已新增 `crash_hard_pct` 参数（近 5 日涨幅硬性禁入，σ 阈值作第二道） |
| P0-3 | 止损成交口径统一（next_open） | ❌ **未实现** | `check_stops` 仍是 bar close 判定 + 同 bar close 成交（LK2）。本方案采用 next_open，见 §2.2 |
| P0-4 | 控制变量对齐 | 计划内 | 四格 + 双资金档共用同一配置基座（池/区间/成本/风控） |

**P0-2 机制说明（回应"要怎么做"）**：已按审计 B7-② 落地，原理是"信号携带预算上限 + 引擎封顶"两层：策略层给网格买点打上 `base_pct_min` 的预算标签（正常做T债务买回不携带、不受影响），引擎层在"空仓重建底仓"分支读取该标签，把本来走完整风控预算（`max_position_pct_per_stock`，可达 40%~100%）的重建量压到试仓档。无需再开发，对比实验可直接受益。

## 2. 后端设计

### 2.1 实验矩阵（核心 4 格 + 1 个加测）

全部核心格共用**同一份 5 分钟数据流与执行引擎**，只差两个策略开关：

| 格 | trend_clock | 做T | 说明 |
|---|---|---|---|
| **A** | `daily` | 开 | 趋势信号只在每日 15:00 bar 评估 → 次日 09:35 开盘成交；T 网格照常全 bar 运行 |
| **B** | `intraday` | 开 | 现状（T-1 特征 + 盘中触发） |
| **C** | `daily` | 关 | A 去掉 T（`max_t_times=0`） |
| **D** | `intraday` | 关 | B 去掉 T |
| **E**（加测） | 纯日线 `period=daily` | 关 | 官方日线数据、15 年窗口（2010-2026）趋势层稳健性参考，不进矩阵 |

**归因设计**（预注册，防事后合理化）：

```
T 边际贡献   = A−C 与 B−D（两列独立估计，互为稳健性检验，两估计不一致→交互显著）
时钟效应     = A−B 与 C−D（两行独立估计）
交互项       = (A−C)−(B−D)
数据源效应   = A−E（日线时钟下，minute5 vs 官方日线的残余差异）
```

### 2.2 P0-3：止损 next_open（本实验前必须实现）

**机制**：`check_stops` 改为 bar i 收盘判定 → 写入 `pending_stops` 队列（优先级高于策略信号）→ bar i+1 开盘成交（含一字跌停检查：无法成交则挂单顺延至下一 bar，直到可成交）。

- 全部四格统一用 next_open（P0-3 决策：不用 eod，保持盘中保护，避免止损口径成为混淆变量）
- 旧 `close` 口径保留为可配置项，仅用于泄漏量对照（A4 已有截断测试护栏）
- 预期影响：止损单多吃 5 分钟缺口（诚实化），四格同受、不破坏归因

### 2.3 momentum_t 策略改造：`trend_clock` 参数

新增 categorical 参数（默认 `intraday` 保持现状）：

```python
{"key": "trend_clock", "type": "categorical", "choices": ["intraday", "daily"], "default": "intraday"}
```

**`daily` 模式的信号门控**：

- 预计算 `is_eod` 列：该 bar 是否当日最后一根（用 shift 比较相邻 bar 的 day；**bar 时间戳属结构性信息，非价格未来信息，无泄漏**）
- `_walk` 中趋势类信号（开仓/加仓/减仓/清仓/试仓升级）仅在 `is_eod=True` 的 bar 上发出 → 走现有 pending 机制 → 次日 09:35 bar 开盘成交
- T 网格信号（做T买卖）不受门控，照常全 bar 运行（阈值用 T-1 的 ATR/vol_pos）
- 实质语义："趋势层是日线决策者（昨日收盘特征+今日收盘评估），T 层是盘中执行者"——与 A1 修复后的系统语义完全自洽

**E 格（period=daily）**：momentum_t 开放 `periods += ["daily"]`，`_walk` 直接迭代日线 bar（日线特征天然 T-1，信号在 D 日 bar 生成 → D+1 日线开盘成交）。注意 walk 内 T 分支在日线模式下自然死路（无 T 数据），`max_t_times=0` 硬关。

### 2.4 实验编排（后端）

**DB**：新增 `experiments` 表（id, name, base_config, variants, capitals, created_at, status）；子任务的 `tasks.payload` 加 `experiment_id` 反向关联。

**任务编排**：`POST /api/experiments` 接收 `{name, base_config, variants, capitals, start_date, end_date}`，按 `变体 × 资金档` 笛卡尔积生成子任务（4×2=8 个），经 TaskManager 进程池顺序执行（3 workers 自然限流），单个子任务失败不影响其它格，experiment 状态聚合计算。

**API 契约**：

| 接口 | 说明 |
|---|---|
| `POST /api/experiments` | 创建实验 → `{experiment_id, sub_task_ids[]}` |
| `GET /api/experiments` | 实验列表（名称/状态/进度/创建时间） |
| `GET /api/experiments/{id}` | 详情：矩阵（每格 task_id + 状态 + metrics 摘要）+ 归因分解 + 基准配置 |

**归因计算在后端做**（不是前端拼）：experiment 完成时从各子任务报告提取 `total_return/sharpe/max_drawdown/t_pnl/commission_total`，按 §2.1 公式算 A−C 等差值，连同两列独立估计的一致性标记一起返回。

### 2.5 对比协议

| 维度 | 设置 |
|---|---|
| 主窗口 | 2026-01~08（与已有三案例对照） |
| 长窗口 | 2020~2026 全 5 分钟区间，切 3 段分别看 |
| E 专属 | 2010~2026 十五年日线 |
| 资金档 | 400k 与 3M 双档（三案例已证明资金敏感性，单档不可信） |
| 指标 | 常规四件套 + t_pnl（周期口径）+ 手续费分解 + 交易笔数 |

### 2.6 预期与决策规则（预注册）

| 观察 | 结论 | 动作 |
|---|---|---|
| A≈C 且 B≈D，T 边际 ≈0 或负 | T 层无净价值 | 砍 T：参数 51→~25 个，过拟合风险大降（审计 OF4 正解） |
| A>C 显著且 B>D 显著 | T 有真实增强 | 保留并优先寻优 T 层 |
| B>A | 盘中触发有价值 | 保留 5 分钟架构 |
| A≥B | 日线时钟已够 | 趋势层可降级日线数据源，省分钟数据维护 |
| 两列 T 估计方向不一致 | T×时钟交互强 | 分别报告，不合并结论 |
| 任一格跨窗口/跨资金档结论翻转 | 路径依赖 | 该格结论降级为不可采信 |

## 3. 前端方案（补充设计）

### 3.1 页面结构

```
路由:
  /experiments            实验列表（新）
  /experiments/:id        实验详情（新）
  /backtests/:id          复用现有结果页（子任务跳转目标）
侧边菜单: "对比实验"（参数寻优下方）
```

### 3.2 实验发起（ExperimentList 页）

复用现有"新建回测"表单作为**基座配置**（策略/池/区间/成本/风控全部沿用现成组件），叠加实验专属区块：

- **实验矩阵勾选**：2×2 可视化勾选卡（daily×T / intraday×T / daily×无T / intraday×无T），默认全选；E 格（纯日线 15 年）单独开关，默认关（跑得久）
- **资金档多选**：Checkbox（400k / 3M / 自定义），默认双档
- 提交 → `POST /api/experiments` → 跳转实验详情页
- 预估提示：按勾选格数×资金档显示"将创建 N 个回测子任务"

### 3.3 实验详情页（ExperimentResult）

**区块一：矩阵总览表**（核心视图）

- 行 = 资金档，列 = 4 个变体（表头两行：时钟 × T 开关）
- 每格显示 `总收益 / 夏普 / 最大回撤` 三行小字，按收益着色（红正绿负）
- 格子状态：pending/running（Progress 迷你条）/failed（红点+错误悬浮）/success（可点击）
- 点击格 → 跳转该子任务 `/backtests/:id` 完整报告

**区块二：归因分解卡**

- 四张 Statistic 卡：`T 边际贡献(A−C)`、`T 边际贡献(B−D)`、`时钟效应(A−B)`、`时钟效应(C−D)`
- 每卡下附一致性标记：两估计同向 ✓（结论可信）/ 异向 ⚠（交互显著，看交互卡）
- 交互项卡 + E 格参考卡（15 年窗口收益，标注"仅趋势层"）

**区块三：分段稳健性条形图**

- 长窗口 3 段 × 4 变体的收益对比（ECharts 分组条形图，复用 EchartsReact 组件）
- 目视检验"跨段结论翻转"

**区块四：基座配置摘要**（折叠）：本实验共用的 config JSON + diff 说明（各格只差 trend_clock / max_t_times / initial_capital）

### 3.4 组件复用与文件清单

| 文件 | 动作 |
|---|---|
| `pages/ExperimentList.tsx` | 新建（表单复用 BacktestList 现有组件：ParamSchemaForm/RiskConfigForm/股票池选择） |
| `pages/ExperimentResult.tsx` | 新建（矩阵表 + 归因卡 + EchartsReact 条形图） |
| `layouts/MainLayout.tsx` | 加"对比实验"菜单项 |
| `App.tsx` | 加两条路由 |
| `api/client.ts` / `types.ts` | 加 createExperiment / listExperiments / getExperiment 与类型 |
| 复用 | TaskStatusTag（格状态）、useTaskProgress 思路改造成 experiment 聚合轮询（详情页 10s 轮询未完成实验） |

### 3.5 交互细节

- 实验未完成时详情页每 10s 轮询（复用 useTaskProgress 的轮询模式），格子实时从灰→running→着色
- 归因卡在 ≥2 格完成时就开始显示（部分归因），全部完成时定格
- 实验列表行：总进度 `x/N`、状态 Tag、创建时间、删除（Popconfirm，级联删子任务）

## 4. 实施顺序

| 阶段 | 内容 | 量级 |
|---|---|---|
| Phase 0 | P0-3 止损 next_open（含跌停顺延）+ 回归测试 | ~半天 |
| Phase 1 | momentum_t `trend_clock` 参数 + is_eod 门控 + E 格 period=daily 开放 | ~1 天 |
| Phase 2 | 后端 experiments API + DB 表 + 编排 | ~半天 |
| Phase 3 | 前端两页 + 菜单路由 | ~1 天 |
| Phase 4 | 跑批（4 格 × 2 资金 × 主窗口 + 长窗口 + E 格 15 年）+ 对比报告 | 跑批时间（minute5 全量约 8×35s/窗口段；E 格日线 15 年较快） |

**验收标准**：

1. 一键发起 8 子任务实验，全程无人工干预完成
2. 归因卡给出 T 边际/时钟效应的双列独立估计 + 一致性标记
3. 依据 §2.6 决策规则产出"砍/留 T 层"与"时钟选择"的明确结论，写入实验报告页

## 5. 风险与注意事项

1. **幸存者偏差**：10 票池为 2026 手工挑选，长窗口结论仅对该池有效，不可外推全市场
2. **量纲核对**：官方日线与 minute5 聚日的 volume/amount 单位一致性需在 Phase 1 验证（两者同源 baostock，预期一致）
3. **E 格与 A 格的数据源差异**：收盘价含集合竞价、日高日低毫厘差——归入 A−E 数据源效应，不视为缺陷
4. **`max_intraday_trades` 对齐**：无 T 格确认风控日内交易限制不误伤止损执行
5. **实验成本**：全矩阵全窗口约 50-80 次引擎运行，按 30-60s/次估 1-1.5 小时机器时间，可过夜跑
