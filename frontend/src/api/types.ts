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
  type: 'int' | 'float' | 'str' | 'bool' | 'select' | 'categorical'
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
  /** 最大持仓只数，0=不限 */
  max_holdings?: number
  /** 现金缓冲比例（永不进场的资金） */
  cash_reserve_pct?: number
}

// ---- 月度出金 ----
export interface WithdrawalConfig {
  /** 每月提取目标额（0=关闭）：不足月末补齐 */
  monthly_withdraw_base?: number
  /** 每笔做T盈利即时提取比例（%） */
  t_profit_withdraw_pct?: number
  /** 做T卖出最小金额（防碎单费用磨损） */
  min_t_amount?: number
}

// ---- 回测任务 ----
export interface BacktestCreateRequest extends WithdrawalConfig {
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
  handling_fee?: number
  regulatory_fee?: number
  exclude_st?: boolean
  /** 指标预热交易日数（0=使用策略建议值） */
  warmup_days?: number
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
  /** 完整回测配置（供「存为模板」复用） */
  config?: BacktestCreateRequest | null
  error?: string | null
}

// ---- 回测配置模板（每用户私有） ----
export interface BacktestTemplateItem {
  id: number
  name: string
  config: BacktestCreateRequest
  created_at: string
  updated_at: string | null
}

export interface TemplateCreateRequest {
  name: string
  config: BacktestCreateRequest
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
  // ---- 出金（落袋为安） ----
  withdrawn_total?: number
  t_profit_withdrawn?: number
  month_topup_withdrawn?: number
  /** 出金覆盖率：足额月份占比（月度目标>0 时） */
  withdrawal_coverage?: number | null
  /** 未补齐的历史缺口累计金额（有缺口时） */
  shortfall_unrecovered?: number | null
  /** 后续月份追偿的历史缺口金额（发生过追偿时） */
  shortfall_recovered?: number | null
}

export interface EquityPoint {
  date: string
  equity: number
  /** 调整净值 = 真实净值 + 累计提取（统计口径基准，出金不算亏损） */
  adjusted_equity?: number
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
  /** 股票名称 */
  name?: string
  volume: number
  cost: number
}

export interface PositionSnapshot {
  date: string
  cash: number
  market_value: number
  positions: PositionSnapshotPosition[]
}

// ---- 出金记录 ----
export interface WithdrawalLogItem {
  month: string
  date: string
  type: 't_profit' | 'month_topup' | 'shortfall' | 'shortfall_recover'
  amount: number
}

export interface WithdrawalSummary {
  monthly_base: number
  total: number
  t_profit: number
  month_topup: number
  /** 未补齐的历史缺口累计金额 */
  shortfall?: number
  /** 后续月份已追偿的历史缺口金额 */
  recover?: number
  months: Record<string, number>
  log: WithdrawalLogItem[]
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
  withdrawal?: WithdrawalSummary
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
  step?: number
  choices?: string[]
}

export interface OptimizeGroupInput {
  name: string
  n_trials: number
  params: Record<string, ParamSpaceItem>
}

export interface OptimizeObjectiveInput {
  metric: OptimizeMetric
  n_windows: number
  variance_penalty: number
  dd_floor?: number | null
}

export interface OptimizeCreateRequest {
  name: string
  backtest_config: BacktestCreateRequest
  param_space?: Record<string, ParamSpaceItem>
  n_trials?: number
  metric?: OptimizeMetric
  /** 方案A：分组坐标轮换（提供时忽略 param_space 平铺搜索） */
  groups?: OptimizeGroupInput[]
  objective?: OptimizeObjectiveInput
  rounds?: number
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
  /** 方案A：分组坐标轮换时标记所属组/轮 */
  group?: string
  round?: number
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

export interface RobustnessCheck {
  verdict: 'robust' | 'fragile' | 'unknown'
  reason?: string | null
  skipped?: string | null
  avg_annual_return_cross_pool?: number | null
  avg_sharpe_cross_pool?: number | null
  avg_annual_return_cross_period?: number | null
  cross_pool?: Array<{
    name: string
    universe: string[]
    annual_return?: number | null
    total_return?: number | null
    max_drawdown?: number | null
    sharpe?: number | null
    skipped?: string | null
  }>
  cross_period?: Array<{
    label: string
    annual_return?: number | null
    total_return?: number | null
    max_drawdown?: number | null
    sharpe?: number | null
    skipped?: string | null
  }>
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
  /** P0 护栏：跨池/跨时段稳健性验证 */
  robustness?: RobustnessCheck | null
  /** 契约未显式列出，但“用最优参数重跑回测”需要，后端若返回则使用 */
  backtest_config?: BacktestCreateRequest | null
  error?: string | null
  // ---- 方案A 分组坐标轮换 ----
  groups_schedule?: Array<{ name: string; n_trials: number; params: Record<string, ParamSpaceItem> }> | null
  objective?: {
    metric: string
    n_windows: number
    variance_penalty: number
    dd_floor?: number | null
  } | null
  rounds_history?: Array<{
    round: number
    best_value: number
    improved: boolean
    groups: Record<string, number>
  }> | null
  per_group_best?: Array<{
    group: string
    round: number
    n_trials: number
    best_value: number
    params: Record<string, ParamValue>
  }> | null
}

// ---- 对比实验（TREN_T_COMPARISON） ----
export type ExperimentCell = 'A' | 'B' | 'C' | 'D' | 'E'

export interface ExperimentCreateRequest {
  name: string
  base_config: BacktestCreateRequest
  cells: ExperimentCell[]
  capitals: number[]
  start_date: string
  end_date: string
  /** 附带 E 格（纯日线 15 年参考，默认关） */
  with_e?: boolean
}

export interface ExperimentMatrixItem {
  task_id: string
  cell: ExperimentCell
  capital: number
  status: TaskStatus
  progress: number
  message?: string | null
  error?: string | null
  metrics?: Record<string, number> | null
}

export interface ExperimentCellAttribution {
  cells?: Record<string, Record<string, number>>
  t_margin_ac?: number | null
  t_margin_bd?: number | null
  clock_ab?: number | null
  clock_cd?: number | null
  interaction?: number | null
  t_consistent?: boolean | null
  clock_consistent?: boolean | null
  /** 各指标（sharpe/回撤/t_pnl/手续费等）的 2×2 差值分解 */
  metrics?: Record<string, {
    t_margin_ac?: number | null
    t_margin_bd?: number | null
    clock_ab?: number | null
    clock_cd?: number | null
    interaction?: number | null
  }>
}

export interface ExperimentDetail {
  experiment_id: string
  name: string
  status: TaskStatus
  progress: number
  error?: string | null
  created_at: string
  finished_at?: string | null
  base_config?: BacktestCreateRequest | null
  cells: ExperimentCell[]
  capitals: number[]
  start_date?: string | null
  end_date?: string | null
  sub_task_ids: string[]
  matrix: ExperimentMatrixItem[]
  attribution: {
    per_capital: Record<string, ExperimentCellAttribution>
    decision: string
  }
}

export interface ExperimentListItem {
  experiment_id: string
  name: string
  cells: ExperimentCell[]
  capitals: number[]
  status: TaskStatus
  progress: number
  error?: string | null
  created_at: string
  finished_at?: string | null
  sub_count: number
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

/** AI 结构化参数建议（LLM 输出末尾 json 块解析而来） */
export interface AiSuggestions {
  params?: Record<string, ParamValue>
  risk_config?: RiskConfig
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
  suggestions?: AiSuggestions | null
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

export interface KeyTestResult {
  ok: boolean
  reply?: string
  model?: string
  elapsed?: number
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
