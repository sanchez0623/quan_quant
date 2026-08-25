// 类型定义：严格按 docs/API_CONTRACT.md 逐字段抄写

export type TaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'
export type Period = 'daily' | 'minute5'
export type OptimizeMetric = 'annual_return' | 'sharpe' | 'calmar' | 'total_return'
export type ParamValue = string | number | boolean

// ---- 认证 ----
export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  expires_in: number
  username: string
}

// ---- 策略 ----
export interface ParamSchema {
  key: string
  label: string
  type: 'int' | 'float' | 'str' | 'bool' | 'select'
  default?: ParamValue
  min?: number
  max?: number
  step?: number
  choices?: string[]
  unit?: string
}

export interface Strategy {
  id: string
  name: string
  description: string
  periods: string[]
  param_schema: ParamSchema[]
}

// ---- 股票查询 ----
export interface StockItem {
  code: string
  name: string
  st: boolean
}

// ---- 风控配置 ----
export interface RiskConfig {
  max_position_pct_per_stock?: number
  max_total_position_pct?: number
  stop_loss_mode?: 'fixed' | 'atr' | 'trailing'
  stop_loss_pct?: number
  atr_period?: number
  atr_multiplier?: number
  take_profit_pct?: number
  trailing_stop_pct?: number
  max_drawdown_breaker?: number
  max_intraday_trades?: number
}

// ---- 回测任务 ----
export interface BacktestCreateRequest {
  name: string
  strategy_id: string
  params: Record<string, ParamValue>
  risk_config?: RiskConfig
  universe: string[]
  start_date: string
  end_date: string
  period: Period
  initial_capital: number
  slippage_pct?: number
  commission_rate?: number
  commission_min?: number
  stamp_tax?: number
  transfer_fee?: number
  exclude_st?: boolean
}

export interface TaskCreateResponse {
  task_id: string
  status: TaskStatus
}

export interface BacktestListItem {
  task_id: string
  name: string
  status: TaskStatus
  created_at: string
  strategy_id: string
  period: string
  error?: string | null
}

export interface TaskStatusResponse {
  task_id: string
  status: TaskStatus
  progress: number
  message?: string | null
  error?: string | null
}

// ---- 回测报告 ----
export interface Metrics {
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  sortino: number
  calmar: number
  win_rate: number
  profit_loss_ratio: number
  total_trades: number
  total_pnl: number
  avg_hold_days: number
  t_trade_count: number
  t_win_rate: number
  t_pnl: number
  open_pnl: number
  add_pnl: number
  reduce_pnl: number
  stop_loss_pnl: number
  commission_total: number
  start_equity: number
  end_equity: number
}

export interface EquityPoint {
  date: string
  equity: number
  drawdown: number
  position_ratio?: number
}

export interface MonthlyReturn {
  year: number
  month: number
  return: number
}

export interface TradeLogItem {
  trade_id: number
  code: string
  name: string
  time: string
  side: 'buy' | 'sell'
  price: number
  volume: number
  amount: number
  fee: number
  type: string
  group_id?: number
  reason?: string | null
  pnl?: number | null
}

export interface PositionSnapshotPosition {
  code: string
  volume: number
  cost: number
}

export interface PositionSnapshot {
  date: string
  cash: number
  market_value: number
  positions: PositionSnapshotPosition[]
}

export interface BacktestReport {
  task_id: string
  name: string
  config: BacktestCreateRequest
  metrics: Metrics
  equity_curve: EquityPoint[]
  monthly_returns: MonthlyReturn[]
  trade_log: TradeLogItem[]
  position_snapshots: PositionSnapshot[]
}

// ---- K线 ----
export interface KLineBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface KLineMark {
  time: string
  price: number
  side: 'buy' | 'sell'
  type: string
  trade_id: number
  /** 前端扩展：由 trade_log 按 trade_id 关联出的交易理由 */
  reason?: string | null
}

export interface KLineResponse {
  code: string
  name: string
  bars: KLineBar[]
  marks: KLineMark[]
}

// ---- 参数寻优 ----
export interface ParamSpaceItem {
  type: 'int' | 'float' | 'select'
  low?: number
  high?: number
  choices?: string[]
}

export interface OptimizeCreateRequest {
  name: string
  backtest_config: BacktestCreateRequest
  param_space: Record<string, ParamSpaceItem>
  n_trials: number
  metric: OptimizeMetric
}

export interface OptimizeListItem {
  task_id: string
  name: string
  status: TaskStatus
  created_at: string
  best_value?: number | null
  best_params?: Record<string, ParamValue> | null
  n_trials: number
}

export interface TrialItem {
  number: number
  params: Record<string, ParamValue>
  value: number
  state: string
  in_sample_value?: number | null
  out_sample_value?: number | null
}

export interface OosMetrics {
  annual_return: number
  max_drawdown: number
  sharpe: number
}

export interface OosValidation {
  in_sample: OosMetrics
  out_sample: OosMetrics
  overfit_risk: 'high' | 'medium' | 'low'
}

export interface OptimizeDetail {
  task_id: string
  /** 契约详情接口未显式列出，后端若返回则展示 */
  name?: string | null
  status: TaskStatus
  progress: number
  metric: OptimizeMetric
  n_trials: number
  best_params?: Record<string, ParamValue> | null
  best_value?: number | null
  trials: TrialItem[]
  param_importance?: Record<string, number> | null
  oos_validation?: OosValidation | null
  /** 契约未显式列出，但“用最优参数重跑回测”需要，后端若返回则使用 */
  backtest_config?: BacktestCreateRequest | null
  error?: string | null
}

// ---- AI 分析 ----
export interface AiProfile {
  name: string
  provider: string
  base_url: string
  model: string
  api_key_env: string
  keys?: number
  available: boolean
}

/** 用户 DB Key 池条目（脱敏，profiles 接口返回） */
export interface UserKeyPoolEntry {
  index: number
  provider: string
  label: string
  base_url: string
  model: string
  key_id: number
  key_label: string
}

export interface AiProfileUsage {
  tokens: number
  calls: number
}

export interface AiProfilesResponse {
  mode: 'db_key_pool' | 'key_pool' | 'profiles'
  user_key_pool?: UserKeyPoolEntry[]
  key_pool?: Array<{ index: number; provider: string; label: string; base_url: string; model: string }>
  providers?: string[]
  profiles: AiProfile[]
  default: string
  usage: {
    total_tokens: number
    total_calls: number
    by_profile: Record<string, AiProfileUsage>
  }
}

export interface AiAnalyzeRequest {
  backtest_id: string
  profile?: string
}

export interface AiAnalysisItem {
  task_id: string
  backtest_id: string
  profile: string
  model: string
  status: TaskStatus
  created_at: string
  content: string | null
  tokens_used: number | null
  elapsed: number | null
  error: string | null
}

// ---- 数据管理 ----
export interface DataDailyStatus {
  rows: number
  stocks: number
  start: string | null
  end: string | null
  updated_at?: string | null
}

export interface DataMinute5Status {
  stocks: number
  rows?: number | null
  start?: string | null
  end?: string | null
  updated_at?: string | null
}

export interface DataAdjFactorStatus {
  rows: number
  updated_at?: string | null
}

export interface DataCalendarStatus {
  start: string | null
  end: string | null
}

export interface DataSourceHealth {
  name: string
  role: string
  healthy: boolean | null
  last_check: string | null
  note: string
}

export interface DataStatus {
  daily: DataDailyStatus
  minute5: DataMinute5Status
  adj_factor: DataAdjFactorStatus
  calendar: DataCalendarStatus
  sources: DataSourceHealth[]
}

export interface DataDemoRequest {
  stocks?: string[]
  days?: number
}

// ---- Key 管理（每用户私有 Key 池） ----
export interface LlmKeyItem {
  id: number
  username: string
  provider: string
  model: string | null
  base_url: string | null
  api_key: string // 脱敏值 sk-***xxxx
  label: string
  sort_order: number
  enabled: boolean
  created_at: string
  updated_at: string | null
}

export interface ProviderRegistryEntry {
  base_url: string
  default_model: string
  label: string
}

export interface KeysResponse {
  keys: LlmKeyItem[]
  providers: string[]
  registry: Record<string, ProviderRegistryEntry>
}

export interface KeyCreateRequest {
  provider: string
  api_key: string
  model?: string | null
  base_url?: string | null
  label?: string
  sort_order?: number
}

export interface KeyUpdateRequest {
  provider?: string
  api_key?: string
  model?: string | null
  base_url?: string | null
  label?: string
  sort_order?: number
  enabled?: boolean
}

// ---- 用户管理 ----
export interface UserItem {
  id: number
  username: string
  created_at: string
}

export interface UserCreateRequest {
  username: string
  password: string
}
