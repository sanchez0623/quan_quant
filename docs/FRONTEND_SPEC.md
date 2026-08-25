# 前端开发规范

必读：先完整阅读 `docs/API_CONTRACT.md`（接口契约，字段名与结构一字不差）与本文档。

## 目标

在 `frontend/` 目录下用 Vite + React + TypeScript + Ant Design 5 + KLineCharts 实现A股量化回测系统前端：登录、回测控制台、结果查看（K线+交易标记）、统计报告、寻优面板、AI 面板、数据管理。

## 技术栈

- Vite 5 + React 18 + TypeScript
- antd 5.x（组件库）、@ant-design/icons
- klinecharts 9.x（K线图，**用 9.x API：init(dsId) 返回实例，registerStyles 等**；买卖点用 overlay mark 实现，或 createOverlay。注意 klinecharts 9 无 `createTooltipKLinePlots`，自定义指标用 registerIndicator）
- echarts 5（资金曲线、回撤、月度热力图、参数重要性条形图、平行坐标图）
- axios、react-router-dom 6、dayjs（antd 自带）
- 状态：轻量用 React Context + hooks 即可，不引 redux

`npm create vite@latest` 脚手架（react-ts 模板）后安装依赖。dev server 端口 5173，`vite.config.ts` 配 proxy：`/api` 与 `/ws` → `http://localhost:8000`（ws 用 `ws: true`）。

## 布局与路由

登录后整体布局：antd Layout，左侧 Sider 菜单（antd 5 无 Menu.Item 子组件写法，用 items prop）：
- 回测中心 `/backtests`（列表+新建）
- 回测结果 `/backtests/:id`
- 参数寻优 `/optimize`（列表+新建） `/optimize/:id`
- AI 分析 `/ai`
- 数据管理 `/data`
顶部 Header：系统名"A股量化回测系统"、当前用户名、退出登录。

路由守卫：无 token → 跳 `/login`。axios 拦截器：401 时清 token 跳登录。

## API 客户端（src/api/client.ts 等）

- `axios.create({ baseURL: '/api' })`，请求拦截器加 `Authorization: Bearer`。
- 契约中所有端点封装为函数，类型定义放 `src/api/types.ts`（从契约逐字段抄 TS interface）。
- WebSocket 工具 `useTaskProgress(taskId, onDone)` hook：`new WebSocket(\`ws://${location.host}/ws/tasks/${id}\`)`（dev 下经 vite proxy；构建后同源）。fallback：WS 失败时每1s轮询 `/api/backtests/{id}/status`。任务终态回调 onDone(status)。

## 页面详细要求

### 1. 登录页 `/login`
用户名/密码 + 登录按钮；调 `/api/auth/login`，token 存 localStorage；错误用 antd message 提示。左侧品牌区+右侧表单卡片，简洁专业（深蓝主色 #1f4e79）。

### 2. 回测中心 `/backtests`
- 顶部"新建回测"抽屉/页面区块（表单）：
  - 名称、策略选择（GET /api/strategies，选中后**根据 param_schema 动态渲染参数表单**：int/float→InputNumber，bool→Switch，select→Select，默认值/单位/范围照 schema）。
  - 股票池：Select mode="multiple" 远程搜索（GET /api/stocks?keyword=，防抖300ms，option 显示 `code name`）；默认提示"输入代码或名称搜索"。
  - 时间区间 RangePicker、周期 Radio（daily/minute5，选项随策略 periods 过滤）、初始资金 InputNumber。
  - 折叠面板"交易成本"：滑点%、佣金率、最低佣金、印花税、过户费（有默认值）。
  - 折叠面板"风控配置"：risk_config 各字段表单（带说明 label：个股仓位上限%/总仓位上限%/止损模式(fixed|atr|trailing)/止损%/ATR周期/ATR倍数/止盈%/移动止损%/最大回撤熔断%/日内交易次数上限）。**风控与策略参数分组展示**。
  - 提交 → POST /api/backtests → message 成功 → 跳转结果页。
- 下方任务列表 Table（倒序）：名称/策略/周期/状态(Tag 色：pending默认/running蓝processing/success绿/failed红)/创建时间/操作(查看、失败原因 Tooltip)。
- 列表页每3s自动刷新（有 running 任务时）。

### 3. 回测结果页 `/backtests/:id`
顶部：任务名+状态；running 时显示 Progress 条 + message（用 WS hook），完成后自动加载报告。
Tabs：
1. **K线图**：股票下拉（universe 内代码）→ GET kline → KLineCharts 渲染：
   - 数据格式转换：bars→KLineData `{timestamp(ms), open, high, low, close, volume, turnover}`。
   - 交易标记：marks 转为自定义 overlay（用 klinecharts `createOverlay` 的简单标记或 registerIndicator 画箭头）：买入▲红色在下、卖出▼绿色在上；不同 type（开仓/加仓/做T/止损/清仓）用不同颜色（开仓红/加仓橙/做T紫/止损绿深）；hover 或 legend 显示 type 与理由（用 overlay 的 onClick 弹 Popover 或 message 展示）。
   - KLineCharts 深色或浅色主题任选，需网格线与十字光标可用。
2. **资金曲线**：ECharts 双图：净值曲线（equity，area 线）+ 回撤曲线（drawdown%，绿色面积向下）；dataZoom 缩放；显示 position_ratio 副轴可选。
3. **统计报告**：
   - 指标卡片行（Statistic 组件网格）：总收益/年化/最大回撤/夏普/索提诺/卡玛/胜率/盈亏比/总交易数/总盈亏/总手续费/期末权益。收益与回撤用百分比格式化（小数→%），红涨绿跌。
   - **做T与加减仓贡献分解卡片**：T交易数/T胜率/T盈亏、开仓盈亏/加仓盈亏/减仓盈亏/止损盈亏（正负着色）。
   - 月度收益热力图（ECharts heatmap：x=月份1-12，y=年份，色带红绿）。
4. **交易明细**：Table（分页、列筛选 type：全部/开仓/加仓/减仓/做T/止损/止盈/清仓；side 筛选可选）：时间/代码/名称/方向(buy红/sell绿)/价格/数量/金额/手续费/类型(Tag)/理由/pnl(平仓行显示)。导出CSV按钮（前端生成）。
5. **持仓快照**：按日 Table（日期/现金/市值/持仓明细展开行）。

### 4. 参数寻优 `/optimize`、`/optimize/:id`
- 新建表单：名称、选择一个已有回测配置作模板（下拉拉 GET /api/backtests，取其 config 预填）、参数搜索空间编辑器：**动态行**（参数名下拉=策略 param_schema 的 key + 风控字段；类型 int/float 时输 low/high，select 时输 choices 逗号分隔）、n_trials(InputNumber, 默认50)、metric(Radio: annual_return/sharpe/calmar/total_return)。
- 列表页：任务名/状态/进度/最优值/创建时间。
- 详情页：running 显示进度与已完成 trials 增量表；success 显示：
  - 最优参数卡片 + 最优值。
  - **样本外验证卡片**（契约 oos_validation）：样本内 vs 样本外 annual_return/max_drawdown/sharpe 对比 + overfit_risk Tag（high红/medium黄/low绿）。
  - 参数重要性条形图（ECharts）。
  - trials 表：number/params(JSON字符串化)/value/state(Tag)/样本内/样本外值。
  - "用最优参数重跑回测"按钮：用 backtest_config + best_params POST /api/backtests 并跳转结果页。

### 5. AI 分析 `/ai`
- 左侧：选择回测任务（下拉，仅 success）+ Profile 选择（GET /api/ai/profiles，只列 available=true 的，标注 default）+ "开始分析"按钮。
- 右侧：分析结果列表（GET /api/ai/analyses?backtest_id=，选中展示）；分析内容 markdown 渲染（**react-markdown**，加依赖）；running 状态用 WS hook 显示进度。
- 底部/侧边：用量统计卡片（total_tokens/total_calls/by_profile）。
- 无可用 profile 时：空态提示"未配置 LLM API Key"。

### 6. 数据管理 `/data`
- 数据水位卡片：daily（股票数/行数/起止日期）、minute5（股票数）、adj_factor、calendar。
- 数据源健康 Table：名称/角色/健康状态(Tag: true绿/false红/null灰"未检测")/最后检查时间/备注。
- 操作区：
  - "增量更新"按钮（Radio 选 daily/minute5/all → POST /api/data/update → 显示任务进度）。
  - "生成演示数据"按钮（POST /api/data/demo，二次确认 Modal：将生成合成数据供演示）→ 进度条 → 完成后刷新卡片。

## 通用

- `npm run build` 必须零错误（TS 严格模式可放宽为默认）。
- 金额/百分比格式化工具函数；表格空数据友好展示。
- 中文 UI 全覆盖。
- 组件拆分到 `src/pages/` 与 `src/components/`（如 KLineChart.tsx 封装 KLineCharts、EquityChart.tsx、HeatmapChart.tsx、ParamSchemaForm.tsx 动态参数表单、TaskStatusTag.tsx）。
- 不需要做 mock；联调阶段连真实后端。**开发时后端可能未就绪，确保构建通过即可**。
- 完成后运行 `npm run build`，确保无 TS 报错。
