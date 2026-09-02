# API 契约（前后端联调统一规范）

Base URL: `http://localhost:8000`，前端开发时代理 `/api` 与 `/ws` 到后端。
认证：除 `/api/auth/login` 外全部接口需要 `Authorization: Bearer <JWT>`。
错误统一格式：`{"detail": "错误描述"}`（FastAPI 默认）。

## 0. 实盘信号机（LIVE\_SIGNAL\_SYSTEM，详见 docs/LIVE\_SIGNAL\_SYSTEM.md）

| 端点                                   | 说明                                                                                                                     |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `POST /api/live/premarket`           | 触发盘前流程（T-1 特征/重选判定/gate/退出检查/飞书推送），返回 `{as_of, health, gate_state, rebalanced, pool, signals, warns, message, pushed}` |
| `GET /api/live/signals?limit&status` | 信号流水（`sig_signal_log`）                                                                                                 |
| `POST /api/live/signals/{id}/status` | 状态变更 `{status}` ∈ 待执行/已成交/已忽略/已过期/信息                                                                                   |
| `POST /api/live/fills`               | 成交回填 `{signal_id?, code, side(buy/sell), fill_price, fill_volume, fee?, note?}` → 联动虚拟持仓 + 关联信号置已成交                    |
| `GET /api/live/positions`            | 虚拟持仓                                                                                                                   |
| `POST /api/live/positions/sync`      | 对账校准 `{positions:[{code,name,volume,cost_price}]}`（以券商为准重建）                                                            |
| `GET/POST /api/live/config`          | 盘前流程参数（above\_ma/rank\_key/top\_x/exit\_need/enter\_th/initial\_capital/suggest\_pct/候选域/t\_mode/max\_holdings...） |
| `GET /api/live/summary`              | 概览（池子/gate/持仓/信号/回填/feishu\_configured/config）                                                                         |
| `POST /api/live/reset`               | 清空信号机数据 `{keep_config}`（信号/回填/持仓/池子/盘中状态机快照/KV）                                                              |
| `POST /api/live/morning`             | **M2 盘前编排任务（异步）**：`{update_data=true, push=true}` → 日线增量更新（含 DATA\_GUARD）→ 盘前流程；返回 `{task_id}`（任务中心查进度）      |
| `POST /api/live/intraday`            | **M2 盘中轮询**：完成 bar → SlotStepper 步进 → 风控前置（T+1/槽位/预算）→ 推送+落库；幂等（bar 游标去重）；返回 `{signals, suspended, no_data, fed_bars, equity, cash, pushed}` |
| `GET /api/live/intraday/status`      | **M2 盘中控制台快照**：各票 qt 现价/状态机状态/喂 bar 游标/心跳（轻量，不拉 K 线）                                                            |
| `POST /api/live/postclose`           | **M2 盘后流程**：当日分钟线合并落库（池子∪持仓∪跟踪）+ 对账卡推送；返回 `{saved, skipped, equity, cash, message, pushed}`                    |
| `GET /api/live/slippage`             | **M3 滑点统计**：回填成交 vs 信号参考价（方向折算为滑点成本）；返回 `{rows, summary{n, avg_slip_pct, buy/sell_avg, worst}}`                |
| `GET /api/live/shadow`               | **M3 影子运行统计**：执行率 + 影子账户（全按参考价足额执行的 FIFO 已实现盈亏）vs 实际回填；返回 `{n_signals, n_filled, fill_rate, shadow_pnl, actual_pnl, gap_pnl, days}` |
| `GET /api/live/readiness`            | **M4 就绪检查**：飞书/数据新鲜/日线覆盖/行情源探测（mootdx/新浪/qt）/t\_mode=off/影子天数≥5/滑点样本≥10/max\_holdings≤5；返回 `{ready, items[{key,label,ok,detail}]}` |

M2 新增持久化：`sig_strategy_state`（SlotStepper 状态快照 + 喂 bar 游标）、`sig_meta`（盘中断流熔断心跳/盘后最后执行时间 KV），均纳入 `POST /reset` 清空范围。

飞书 webhook 从 `.env` 的 `FEISHU_WEBHOOK_URL` 读取（不入库）；未配置时推送静默跳过。

## 1. 认证

### POST /api/auth/login

请求：`{"username": "admin", "password": "..."}`
响应：`{"token": "<jwt>", "expires_in": 86400, "username": "admin"}`
错误：401 `{"detail": "用户名或密码错误"}`

## 2. 策略

### GET /api/strategies

响应：

```json
[
  {
    "id": "ma_cross",
    "name": "双均线策略",
    "description": "快线上穿慢线买入，下穿卖出",
    "periods": ["daily", "minute5"],
    "param_schema": [
      {"key": "fast", "label": "快线周期", "type": "int", "default": 5, "min": 2, "max": 60},
      {"key": "slow", "label": "慢线周期", "type": "int", "default": 20, "min": 5, "max": 250},
      {"key": "stop_loss_pct", "label": "止损比例", "type": "float", "default": 8.0, "min": 1, "max": 50, "step": 0.5, "unit": "%"}
    ]
  }
]
```

param\_schema 条目字段：key/label/type(int|float|str|bool|select)/default/min/max/step/choices/unit，均可选（除 key/label/type/default）。

## 3. 股票查询

### GET /api/stocks?keyword=600\&limit=20

响应：`[{"code": "600000", "name": "浦发银行", "st": false}]`
（本地 stock\_basic.parquet 模糊匹配 code 或 name）

### GET /api/stocks/pick-options

条件选股维度选项：`{"indices":[{"key","name","count"}],"industry_tree":[申万L1→L2→L3树(带count)],
"boards":[{"key":"main|chinext|star|bse","name","count"}],"industry_snapshot","index_snapshot"}`

### POST /api/stocks/pick

条件选股（即时查询）：静态过滤 + 随机抽样，或动量趋势预筛。

```json
{
  "filters": {
    "index": ["hs300", "zz500"],
    "industry_l1": [], "industry_l2": [], "industry_l3": [],
    "boards": ["chinext"],
    "exclude_st": true,
    "momentum": {"top_x": 30, "above_ma": 60, "with_accel": false, "min_rps": null, "rank_key": "score"}
  },
  "random": {"n": 20, "seed": 42},
  "as_of": "2025-01-01"
}
```

- `index`：单字符串（历史兼容）或数组；**数组=并集**（沪深300+中证500 直接多选）。

- `momentum` 传入即走动量趋势预筛（MOMENTUM\_CORE 与策略同口径：门槛\[金叉+站上均线+动量为正+崩溃保护内置]→全市场RPS→按分排序→取前x）；此时 `as_of` 必填（传回测开始日，后端取**严格早于它的最近交易日**为基准日，无后视镜），RPS 恒为全市场口径，指数/行业/板块作为候选域叠加。`rank_key`（排序键）∈ {score=累计强度(默认) / accel=加速度 / fresh=金叉新鲜度 / mom\_gap=短中差值}：门槛不变，只改过门槛者的座次，非法值 400。

- 响应：`{"codes","name_map","total_matched","total_picked","seed_used","truncated","meta", "items"?}`；
  动量预筛额外返回 `items:[{rank,code,name,score,rps}]`（按 rank 排序的分数明细），meta 带 `snapshot_date`（实际基准日）与 `momentum` 参数。

## 4. 回测任务

### POST /api/backtests

请求：

```json
{
  "name": "测试回测",
  "strategy_id": "ma_cross",
  "params": {"fast": 5, "slow": 20},
  "risk_config": {
    "max_position_pct_per_stock": 30,
    "max_total_position_pct": 100,
    "stop_loss_mode": "fixed",
    "stop_loss_pct": 8.0,
    "atr_period": 14,
    "atr_multiplier": 2.5,
    "take_profit_pct": 0,
    "trailing_stop_pct": 0,
    "max_drawdown_breaker": 30,
    "max_intraday_trades": null
  },
  "universe": ["600000", "000001"],
  "universe_meta": null,
  "universe_auto": false,
  "auto_idle_days": 5,
  "auto_top_x": 30,
  "auto_above_ma": 60,
  "auto_with_accel": false,
  "auto_min_rps": null,
  "auto_index": [],
  "auto_boards": [],
  "auto_rank_key": "score",
  "benchmark": "000905",
  "start_date": "2023-01-01",
  "end_date": "2024-12-31",
  "period": "daily",
  "initial_capital": 1000000,
  "slippage_pct": 0.001,
  "commission_rate": 0.0003,
  "commission_min": 5,
  "stamp_tax": 0.001,
  "transfer_fee": 0.00001,
  "exclude_st": true
}
```

risk\_config 全字段可选（有默认值）。`max_intraday_trades` 传 `null`/缺省时自动对齐策略参数 `max_t_times`（策略无该参数则兜底 4）。响应：`{"task_id": "bt_xxx", "status": "pending"}`

动态选股（DYNAMIC\_SELECT，仅 momentum\_t/momentum\_slot）：

- `universe_auto=true` 时 `universe` 必须留空（校验 400）：每段池子由动量趋势预筛自动生成——基准日=严格早于段首的最近交易日（无后视镜 T-1）；

- **滚动重选**：全空仓持续 `auto_idle_days` 个交易日 → 以触发日收盘为基准重筛，旧池退役、新池次日接管；全市场（候选域内）无票过门槛 → 空仓现金推进，绝不硬买；

- 候选域：`auto_index`（指数成分**并集**，sz50/hs300/zz500/csi800）∩ `auto_boards`（板块并集 main/chinext/star/bse），均空=全市场剔ST/退市；域内无票则初始池报错、中途无票则空池等待（不回退全市场）；

- `auto_above_ma`=站上均线锚周期（60 对齐 momentum\_t / 20 对齐 momentum\_slot）；`auto_with_accel`=动量分叠加加速度项（对齐 momentum\_slot）；`auto_min_rps`=全市场 RPS 分位下限（0\~100，null 不启用）；`auto_rank_key`（重选排序键）∈ {score/accel/fresh/mom\_gap}，语义与选股器 `momentum.rank_key` 相同（score=累计强度默认 / accel=加速度 / fresh=金叉新鲜度 / mom\_gap=短中差值），用于让动态重选偏向「刚开始涨」的票（门槛不变，只改座次）。

### 基准对比（BENCHMARK，全策略通用）

- `benchmark`（基准指数）∈ {000905=中证500(默认) / 000300=沪深300}；需先在数据管理页拉取指数日线（scope=index\_daily，独立 index\_daily.parquet，与个股数据隔离）；

- 报告生成时按 equity\_curve 日期对齐指数收盘（缺失日前值填充），归一化到初始资金：报告新增 `benchmark={index_key,name,curve:[{date,close,equity}],return}`；metrics 新增 `benchmark_return`（同期基准收益）与 `excess_return`（超额=策略−基准），均小数口径；

- 指数数据缺失（未拉取/区间不覆盖）时**静默降级**：不写 benchmark、不加指标，回测不受影响。

### GET /api/backtests

响应：`[{"task_id","name","status(pending|running|success|failed)","created_at","strategy_id","period","config(完整回测配置,供存为模板)","error}]`（倒序）

### GET /api/backtests/templates

响应：`[{"id","name","config(BacktestRequest 同构)","created_at","updated_at"}]`（当前用户私有，倒序）

### POST /api/backtests/templates

请求：`{"name":"我的标准配置","config":{...BacktestRequest 同构...}}`
响应：`{"id":1,"status":"ok"}`（config 缺 strategy\_id 时 400）

### DELETE /api/backtests/templates/{template\_id}

响应：`{"status":"ok"}`（非属主 404）

### GET /api/backtests/{task\_id}/status

响应：`{"task_id","status","progress": 0~100,"message":"回测中: 600000","error": null}`

### WebSocket /ws/tasks/{task\_id}

推送消息（每0.5s有变化才推）：`{"status":"running","progress":45,"message":"..."}`，结束时推 `{"status":"success","progress":100}` 后关闭。

### GET /api/backtests/{task\_id}/report

响应（success 状态才有完整数据，否则 400/404）：

```json
{
  "task_id": "bt_xxx",
  "name": "测试回测",
  "config": { ...提交时的完整配置... },
  "metrics": {
    "total_return": 0.234, "annual_return": 0.112, "max_drawdown": -0.156,
    "sharpe": 1.23, "sortino": 1.45, "calmar": 0.72,
    "win_rate": 0.55, "profit_loss_ratio": 1.8, "total_trades": 120,
    "total_pnl": 234000, "avg_hold_days": 3.2,
    "t_trade_count": 40, "t_win_rate": 0.5, "t_pnl": -5000,
    "open_pnl": 100000, "add_pnl": 50000, "reduce_pnl": 30000, "stop_loss_pnl": -80000,
    "commission_total": 12000, "start_equity": 1000000, "end_equity": 1234000
  },
  "equity_curve": [{"date":"2023-01-03","equity":1000234,"drawdown":0,"position_ratio":0.5}],
  "monthly_returns": [{"year":2023,"month":1,"return":0.032}],
  "trade_log": [
    {"trade_id":1,"code":"600000","name":"浦发银行","time":"2023-01-05","side":"buy",
     "price":10.5,"volume":10000,"amount":105000,"fee":36.5,
     "type":"开仓","group_id":1,"reason":"MA5上穿MA20","pnl":null,
     "t_mode":null,"seg":null}
  ],
  "position_snapshots": [{"date":"2023-01-05","cash":500000,"market_value":500234,"positions":[{"code":"600000","volume":10000,"cost":10.5}]}],
  "engine_version": "t_refactor_v1",
  "t_open_debts": [],
  "t_reject_events": [],
  "universe_auto": false,
  "auto_segments": []
}
```

trade\_log 的 type 枚举：`开仓/加仓/减仓/做T/止损/止盈/清仓`；side：`buy/sell`；平仓记录 pnl 有值（该笔平仓对应持仓的实现盈亏），开仓记录 pnl=null。

- `t_mode`：做T交易携带机制标记（grid/discipline/time/off，T\_REFACTOR 配对口径）；

- `seg`：动态选股段号（universe\_auto 分段滚动重选时标记归属段，静态池回测为 null）；

- `engine_version`：`t_refactor_v1` = t\_pnl 配对口径（闭环价差+期末未闭环浮亏计提），与旧版结果不可比；

- `t_open_debts`：期末未闭环做T债务（mark-to-market 浮亏已计提进 t\_pnl）；`t_reject_events`：追回/回补被拒事件（审计可见，不进 trade\_log）；

- `universe_auto=true` 时 `auto_segments` 非空：`[{seg,start,end,as_of,universe,picked:[{rank,code,name,score,rps}],trigger_day?,trigger_reason?,next_picked?}]`（每段池子来历与重选触发点）。

### GET /api/backtests/{task\_id}/kline?code=600000

响应：

```json
{
  "code": "600000", "name": "浦发银行",
  "bars": [{"date":"2023-01-03","open":10.0,"high":10.2,"low":9.9,"close":10.1,"volume":123456}],
  "marks": [{"time":"2023-01-05","price":10.5,"side":"buy","type":"开仓","trade_id":1}]
}
```

bars 的 date 格式：daily 为 `YYYY-MM-DD`，minute5 为 `YYYY-MM-DD HH:mm`。

## 5. 参数寻优（Optuna）

### POST /api/optimize

请求：

```json
{
  "name": "寻优1",
  "backtest_config": { ...与 POST /api/backtests 相同的完整配置，params 中可包含被搜索参数的初始值... },
  "param_space": {
    "fast": {"type":"int","low":3,"high":15},
    "stop_loss_pct": {"type":"float","low":3,"high":12}
  },
  "n_trials": 50,
  "metric": "annual_return"
}
```

响应：`{"task_id": "opt_xxx", "status": "pending"}`
metric 可选：annual\_return / sharpe / calmar / total\_return（默认 annual\_return）。

**分组坐标轮换格式（推荐，方案A）**：传 `groups` 时替代 `param_space` 平铺——

```json
{
  "name": "分层寻优",
  "backtest_config": { ...完整回测配置... },
  "groups": [
    {"name": "选股排序", "n_trials": 30,
     "params": {"mom_short": {"type":"int","low":5,"high":20,"step":5},
                "exit_need": {"type":"categorical","choices":[1,2,3]}}}
  ],
  "rounds": 2,
  "objective": {"metric": "total_return", "n_windows": 3,
                 "variance_penalty": 0.5, "dd_floor": -0.35}
}
```

- 每轮只搜一组参数（其它组固定当前最优）；`objective.n_windows` 把样本内切窗评估（score = 均值 − λ×跨窗std，任一窗回撤击穿 `dd_floor` 重罚），防单窗口过拟合；

- 总试验预算 = Σ每组 n\_trials × rounds，上限 2000；

- **约束**：`backtest_config.universe_auto` 必须为 false（动态选股池由引擎运行时生成，寻优依赖静态池；前端选模板时会自动把动态池固化为原回测各段实际交易股票的并集）；

- 执行模型（P0-3/P1-3）：每批 trial（默认 5 个）在全新子进程中执行，批结束进程退出由 OS 回收内存（防任务内 OOM）；批内被系统杀死自动减半重试；单 trial 回测异常按 FAIL 记账继续，不放大为任务失败；

- **trial 并行度**：env `OPTIMIZE_PARALLEL_TRIALS`（默认 1=串行批处理；设 2/3 时每组 trial 由多个子进程波次并发执行，注意 并行度×单trial内存峰值 需留足物理内存）。

### POST /api/optimize/{task\_id}/resume

断点续传：以同一 task\_id 重新提交（Optuna load\_if\_exists 载入既有 trial，只补跑剩余）。进程中断/死机后任务停在 running/pending/failed 时使用。
响应：`{"task_id": "opt_xxx", "status": "pending"}`

### GET /api/optimize

寻优任务列表：`[{"task_id","name","status","created_at","best_value","best_params","n_trials"}]`

### GET /api/optimize/{task\_id}

响应：

```json
{
  "task_id": "opt_xxx", "status": "success", "progress": 100,
  "metric": "annual_return", "n_trials": 50,
  "best_params": {"fast": 7, "stop_loss_pct": 6.5}, "best_value": 0.23,
  "trials": [
    {"number":0,"params":{"fast":5},"value":0.12,"state":"complete",
     "in_sample_value":0.15,"out_sample_value":0.10}
  ],
  "param_importance": {"fast": 0.7, "stop_loss_pct": 0.3},
  "oos_validation": {"in_sample": {"annual_return":0.25,"max_drawdown":-0.1,"sharpe":1.5},
                      "out_sample": {"annual_return":0.08,"max_drawdown":-0.14,"sharpe":0.6},
                      "overfit_risk": "high|medium|low"},
  "error": null
}
```

（status 为 running 时 trials 为已完成部分；param\_importance/oos\_validation 完成后才有值）

## 6. AI 分析（多 LLM）

### GET /api/ai/profiles

响应：

```json
{
  "profiles": [
    {"name":"main","provider":"openai_compatible","base_url":"https://api.siliconflow.cn/v1",
     "model":"deepseek-ai/DeepSeek-V3","api_key_env":"SILICONFLOW_API_KEY","available":true}
  ],
  "default": "main",
  "usage": {"total_tokens": 12345, "total_calls": 10, "by_profile": {"main": {"tokens": 12000, "calls": 9}}}
}
```

available = 对应环境变量已配置。

### DELETE /api/ai/usage

响应：`{"status":"ok"}`（清空 llm\_usage 用量统计，如清除测试脏数据）

### POST /api/ai/analyze

请求：`{"backtest_id": "bt_xxx", "profile": "main"}`（profile 可选，默认 default）
响应（异步任务）：`{"task_id": "ai_xxx", "status": "pending"}`
进度同样走 GET /api/backtests/{task\_id}/status 与 WS /ws/tasks/{task\_id}（status 枚举一致）。

### GET /api/ai/analyses?backtest\_id=bt\_xxx

响应：

```json
[{"task_id":"ai_xxx","backtest_id":"bt_xxx","profile":"main","model":"...",
  "status":"success","created_at":"...",
  "content": "## 策略诊断\n...(markdown)","tokens_used": 3500, "elapsed": 12.3, "error": null,
  "suggestions": {"params": {"fast": 10}, "risk_config": {"stop_loss_pct": 12}}}]
```

suggestions 为 LLM 输出末尾 \`\`\`json 块解析出的结构化参数建议（已过滤非法字段；无可调参数时为 null），
前端用于「应用建议并创建下一轮回测」：与原回测 config 合并后预填回测表单。

## 7. 数据管理

### GET /api/data/status

响应：

```json
{
  "daily": {"rows": 6600000, "stocks": 5400, "start": "2021-01-04", "end": "2025-12-30", "updated_at": "..."},
  "minute5": {"stocks": 5400, "updated_at": "..."},
  "adj_factor": {"rows": 6600000, "updated_at": "..."},
  "calendar": {"start": "2021-01-04", "end": "2025-12-30"},
  "sources": [
    {"name": "baostock", "role": "daily主源", "healthy": true, "last_check": "...", "note": ""},
    {"name": "mootdx", "role": "minute5主源", "healthy": null, "last_check": null, "note": "未安装，可选依赖"}
  ]
}
```

### POST /api/data/update

请求：`{"scope": "daily"}`（daily | minute5 | all | industry | stock\_basic | calendar | index\_daily，默认 daily；index\_daily=基准指数日线 000905/000300，独立存储）
响应：`{"task_id": "data_xxx", "status": "pending"}`（异步任务，进度走同一 status/WS 通道）

### POST /api/data/demo

请求：`{"stocks": ["600000","000001"], "days": 500}`（可选，有默认值）
响应：`{"task_id": "data_xxx", "status": "pending"}`
生成合成演示数据（随机游走+趋势），用于无真实数据源环境的联调与演示。幂等：重复调用覆盖生成。

## 8. 通用约定

- 所有日期字符串 `YYYY-MM-DD`；分钟时间 `YYYY-MM-DD HH:mm`。

- 百分比字段：risk\_config 与 param\_schema 中用百分数值（8.0 表示 8%）；metrics 中收益率/回撤用小数（0.234 表示 23.4%）。

- 任务状态机：pending → running → success | failed | cancelled。

- 响应自 commit b815760 起启用 GZip 压缩（>1KB 的 JSON 自动压缩，浏览器/客户端透明解压）。

- 服务端 env 配置（`.env`）：`ENABLE_SCHEDULER`（每日16:10定时数据更新，默认0）、
  `OPTIMIZE_PARALLEL_TRIALS`（寻优 trial 并行度，默认1；内存充足可设 2\~3，并行度×单trial内存峰值需留足物理内存）。

- 进度接口对不存在任务返回 404。

- WebSocket 连接：`ws://localhost:8000/ws/tasks/{task_id}`，无需 JWT（任务id本身是随机不可猜的）。

