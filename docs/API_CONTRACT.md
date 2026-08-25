# API 契约（前后端联调统一规范）

Base URL: `http://localhost:8000`，前端开发时代理 `/api` 与 `/ws` 到后端。
认证：除 `/api/auth/login` 外全部接口需要 `Authorization: Bearer <JWT>`。
错误统一格式：`{"detail": "错误描述"}`（FastAPI 默认）。

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
param_schema 条目字段：key/label/type(int|float|str|bool|select)/default/min/max/step/choices/unit，均可选（除 key/label/type/default）。

## 3. 股票查询

### GET /api/stocks?keyword=600&limit=20
响应：`[{"code": "600000", "name": "浦发银行", "st": false}]`
（本地 stock_basic.parquet 模糊匹配 code 或 name）

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
    "atr_multiplier": 2.0,
    "take_profit_pct": 0,
    "trailing_stop_pct": 0,
    "max_drawdown_breaker": 30,
    "max_intraday_trades": 4
  },
  "universe": ["600000", "000001"],
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
risk_config 全字段可选（有默认值）。响应：`{"task_id": "bt_xxx", "status": "pending"}`

### GET /api/backtests
响应：`[{"task_id","name","status(pending|running|success|failed)","created_at","strategy_id","period","error}]`（倒序）

### GET /api/backtests/{task_id}/status
响应：`{"task_id","status","progress": 0~100,"message":"回测中: 600000","error": null}`

### WebSocket /ws/tasks/{task_id}
推送消息（每0.5s有变化才推）：`{"status":"running","progress":45,"message":"..."}`，结束时推 `{"status":"success","progress":100}` 后关闭。

### GET /api/backtests/{task_id}/report
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
     "type":"开仓","group_id":1,"reason":"MA5上穿MA20","pnl":null}
  ],
  "position_snapshots": [{"date":"2023-01-05","cash":500000,"market_value":500234,"positions":[{"code":"600000","volume":10000,"cost":10.5}]}]
}
```
trade_log 的 type 枚举：`开仓/加仓/减仓/做T/止损/止盈/清仓`；side：`buy/sell`；平仓记录 pnl 有值（该笔平仓对应持仓的实现盈亏），开仓记录 pnl=null。

### GET /api/backtests/{task_id}/kline?code=600000
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
metric 可选：annual_return / sharpe / calmar / total_return（默认 annual_return）。

### GET /api/optimize
寻优任务列表：`[{"task_id","name","status","created_at","best_value","best_params","n_trials"}]`

### GET /api/optimize/{task_id}
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
（status 为 running 时 trials 为已完成部分；param_importance/oos_validation 完成后才有值）

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

### POST /api/ai/analyze
请求：`{"backtest_id": "bt_xxx", "profile": "main"}`（profile 可选，默认 default）
响应（异步任务）：`{"task_id": "ai_xxx", "status": "pending"}`
进度同样走 GET /api/backtests/{task_id}/status 与 WS /ws/tasks/{task_id}（status 枚举一致）。

### GET /api/ai/analyses?backtest_id=bt_xxx
响应：
```json
[{"task_id":"ai_xxx","backtest_id":"bt_xxx","profile":"main","model":"...",
  "status":"success","created_at":"...",
  "content": "## 策略诊断\n...(markdown)","tokens_used": 3500, "elapsed": 12.3, "error": null}]
```

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
请求：`{"scope": "daily"}`（daily | minute5 | all，默认 daily）
响应：`{"task_id": "data_xxx", "status": "pending"}`（异步任务，进度走同一 status/WS 通道）

### POST /api/data/demo
请求：`{"stocks": ["600000","000001"], "days": 500}`（可选，有默认值）
响应：`{"task_id": "data_xxx", "status": "pending"}`
生成合成演示数据（随机游走+趋势），用于无真实数据源环境的联调与演示。幂等：重复调用覆盖生成。

## 8. 通用约定

- 所有日期字符串 `YYYY-MM-DD`；分钟时间 `YYYY-MM-DD HH:mm`。
- 百分比字段：risk_config 与 param_schema 中用百分数值（8.0 表示 8%）；metrics 中收益率/回撤用小数（0.234 表示 23.4%）。
- 任务状态机：pending → running → success | failed | cancelled。
- 进度接口对不存在任务返回 404。
- WebSocket 连接：`ws://localhost:8000/ws/tasks/{task_id}`，无需 JWT（任务id本身是随机不可猜的）。
