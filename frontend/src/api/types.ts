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
  /** 参数说明（Tooltip 展示） */
  description?: string
  /** 分组名：影响同一类回测结果的参数归并展示，按 schema 出现顺序排列 */
  group?: string
  /** 高级参数：组内默认收起，需手动展开（二次微调项，非核心） */
  advanced?: boolean
  /** 冻结参数：默认值即推荐值，表单灰显且不进入寻优空间（PARAM_FREEZE 约定） */
  frozen?: boolean
  /** 条件显示：{ 依赖参数key: 允许的值数组 }，全部满足才显示；依赖值未设置时不隐藏 */
  show_if?: Record<string, (string | number)[]>
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

// ---- 条件选股（UNIVERSE_PICKER）----
/** 条件选股溯源：池子的来历与 seed，随 config 存入模板/report（方案 §7） */
export interface UniverseMeta {
  source: string
  filters: {
    /** 指数成分（多选=并集；历史模板可能为单字符串） */
    index?: string | string[] | null
    industry_l1?: string[]
    industry_l2?: string[]
    industry_l3?: string[]
    boards?: string[]
    exclude_st?: boolean
  }
  seed_used?: number | null
  total_matched?: number
  total_picked?: number
  picked_at?: string
  /** 动量预筛：请求的基准日（=回测开始日） */
  as_of_requested?: string
  /** 动量预筛：实际基准日（严格早于开始日的最近交易日，无后视镜） */
  snapshot_date?: string
  /** 动量预筛参数（与动态选股 universe_auto 同构） */
  momentum?: MomentumPickInput
}

export interface PickIndexOption {
  key: string
  name: string
  count: number
}

export interface PickBoardOption {
  key: string
  name: string
  count: number
}

export interface PickIndustryNode {
  value: string
  label: string
  count: number
  children?: PickIndustryNode[]
}

export interface PickOptions {
  indices: PickIndexOption[]
  industry_tree: PickIndustryNode[]
  boards: PickBoardOption[]
  industry_snapshot: string | null
  index_snapshot: string | null
}

/** 动量趋势预筛参数（选股器与动态选股 universe_auto 同一套口径） */
export interface MomentumPickInput {
  /** 排序后取前 x 只 */
  top_x: number
  /** 站上均线锚周期（60 对齐 momentum_t / 20 对齐 momentum_slot） */
  above_ma: number
  /** 动量分叠加加速度项（对齐 momentum_slot） */
  with_accel: boolean
  /** 全市场 RPS 分位下限（0~100，null=不启用） */
  min_rps?: number | null
  /** 排序键：score=累计强度 / accel=加速度 / fresh=金叉新鲜度 / mom_gap=短中差值 */
  rank_key?: string
}

/** 动量预筛结果明细（带分数，展示"为什么选它"） */
export interface MomentumPickItem {
  rank: number
  code: string
  name: string
  score: number
  /** 0~100，全市场分位 */
  rps: number | null
}

export interface PickFiltersInput {
  /** 指数成分（多选=并集） */
  index?: string[] | null
  industry_l1?: string[]
  industry_l2?: string[]
  industry_l3?: string[]
  boards?: string[]
  exclude_st?: boolean
  momentum?: MomentumPickInput | null
}

export interface PickRandomInput {
  n?: number | null
  seed?: number | null
}

export interface PickRequest {
  filters: PickFiltersInput
  random?: PickRandomInput | null
  /** 动量预筛基准日（传回测开始日，后端取其前一交易日） */
  as_of?: string | null
}

export interface PickResponse {
  codes: string[]
  name_map: Record<string, string>
  total_matched: number
  total_picked: number
  seed_used?: number | null
  truncated?: boolean
  meta: UniverseMeta
  /** 动量预筛结果明细（按 rank 排序） */
  items?: MomentumPickItem[]
}

// ---- 风控配置 ----
export interface RiskConfig {
  max_position_pct_per_stock?: number
  max_total_position_pct?: number
  stop_loss_mode?: 'fixed' | 'atr' | 'trailing' | 'atr_trailing'
  stop_loss_pct?: number
  atr_period?: number
  /** atr / atr_trailing：成本项 ATR 倍数 k1 */
  atr_multiplier?: number
  take_profit_pct?: number
  trailing_stop_pct?: number
  max_drawdown_breaker?: number
  max_intraday_trades?: number
  /** 最大持仓只数，0=不限 */
  max_holdings?: number
  /** 现金缓冲比例（永不进场的资金） */
  cash_reserve_pct?: number
  // ---- atr_trailing：止损线 = max(成本−k1×ATR, 最高价−k2×ATR)，只上不下 ----
  /** 移动锁盈倍数 k2（相对持仓期最高价） */
  atr_trail_mult?: number
  /** 成本基准：first=首笔开仓价（不受加仓抬高）｜wavg=加权平均成本 */
  atr_cost_base?: 'first' | 'wavg'
  /** 棘轮：止损线只上不下 */
  atr_trail_floor?: number
  // ---- 自适应止损：按市场状态缩放 k1/k2 ----
  adaptive?: 'off' | 'trend' | 'vol'
  adaptive_trend_ma?: number
  adaptive_slope_n?: number
  /** 趋势确立时 k1/k2 的放大倍数（放宽止损，让利润奔跑） */
  adaptive_k_loose?: number
  /** 跌破均线时 k1/k2 的缩小倍数（收紧止损，快速离场） */
  adaptive_k_tight?: number
  adaptive_vol_n?: number
  adaptive_vol_hi?: number
  adaptive_vol_lo?: number
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
  /** 动态选股开启时留空（池子由动量预筛自动生成并滚动重选） */
  universe: string[]
  /** 条件选股溯源（方案 §7）：池子来历与 seed，模板载入/实验复现可审计 */
  universe_meta?: UniverseMeta | null
  // ---- 动态选股（universe_auto，仅 momentum_t/momentum_slot）----
  universe_auto?: boolean
  /** 全空仓持续 N 个交易日 -> 重选 */
  auto_idle_days?: number
  /** 每次预筛取前 x 只 */
  auto_top_x?: number
  /** 站上均线锚周期 */
  auto_above_ma?: number
  auto_with_accel?: boolean
  auto_min_rps?: number | null
  /** 候选域：指数成分并集（空=不限） */
  auto_index?: string[]
  /** 候选域：板块并集（空=不限，与指数域取交集） */
  auto_boards?: string[]
  /** 重选排序键：score=累计强度 / accel=加速度 / fresh=金叉新鲜度 / mom_gap=短中差值 */
  auto_rank_key?: string
  /** 基准指数：000905=中证500 / 000300=沪深300（报告净值图叠加 + 超额指标） */
  benchmark?: string
  /** 池级趋势开关：池内动量健康度过低时抑制开仓/加仓 */
  pool_gate?: boolean
  /** gate 触发阈值（健康度占比），恢复线=×2 内置 */
  pool_gate_enter_th?: number
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
  /** 已闭环做T价差（旧周期口径对照，T_REFACTOR 配对口径） */
  t_pnl_closed?: number | null
  /** 做T盈亏比（平均盈利/|平均亏损|） */
  t_payoff?: number | null
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
  // ---- 基准对比（BENCHMARK，指数数据缺失时缺省） ----
  /** 同期基准指数收益（小数口径） */
  benchmark_return?: number | null
  /** 超额收益 = total_return - benchmark_return */
  excess_return?: number | null
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
  /** 做T机制标记（grid/discipline/time/off，T_REFACTOR） */
  t_mode?: string | null
  /** 动态选股段号（universe_auto 分段滚动重选时标记归属段） */
  seg?: number
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

/** 动态选股（universe_auto）段元信息：每段的池子来历与重选触发点 */
export interface AutoSegmentInfo {
  seg: number
  /** 段起始交易日 */
  start: string
  /** 段结束交易日（=下一触发日或回测结束日） */
  end: string
  /** 本段池子的预筛基准日（T-1，无后视镜） */
  as_of: string
  universe: string[]
  /** 预筛明细（rank/code/name/score/rps） */
  picked: MomentumPickItem[]
  /** 触发重选的交易日（末段无） */
  trigger_day?: string
  /** 触发原因（如"全空仓持续5个交易日"） */
  trigger_reason?: string
  /** 触发后重选出的下一池明细（空=全市场无票过门槛） */
  next_picked?: MomentumPickItem[]
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
  /** 引擎版本（t_refactor_v1 = 做T配对口径，与旧版结果不可比） */
  engine_version?: string
  /** 期末未闭环做T债务（mark-to-market 浮亏已计提进 t_pnl） */
  t_open_debts?: TOpenDebt[]
  /** 追回/回补被拒事件（审计可见，不进 trade_log） */
  t_reject_events?: TRejectEvent[]
  /** 动态选股：分段滚动重选段元信息 */
  universe_auto?: boolean
  auto_segments?: AutoSegmentInfo[]
  /** 基准对比（BENCHMARK，指数数据缺失时缺省）：归一化到初始资金的指数净值 */
  benchmark?: BenchmarkInfo
}

/** 基准指数对比（按 equity_curve 日期对齐，缺失日前值填充） */
export interface BenchmarkInfo {
  index_key: string
  name: string
  return: number
  curve: Array<{ date: string; close: number; equity: number }>
}

// ---- 实盘信号机（LIVE_SIGNAL_SYSTEM）----
export interface LiveSignalItem {
  id: number
  ts: string
  kind: 'premarket' | 'intraday'
  code: string | null
  name: string
  stype: string
  reason: string
  suggest_amount: number | null
  ref_price: number | null
  status: string
  extra: Record<string, unknown>
  created_at: string
}

export interface LivePosition {
  code: string
  name: string
  volume: number
  cost_price: number
  open_day: string | null
  group_id: number | null
  last_price: number | null
  last_ts: string | null
  updated_at: string
}

export interface LivePoolState {
  pool: Array<{ code: string; name?: string }>
  as_of: string | null
  gate_state: number
  health_history: Array<{ day: string; health: number }>
  idle_start: string | null
  updated_at: string | null
}

export interface LiveFill {
  id: number
  signal_id: number | null
  code: string
  side: 'buy' | 'sell'
  fill_price: number
  fill_volume: number
  fee: number
  fill_time: string | null
  note: string
  created_at: string
}

export interface LiveConfig {
  above_ma: number
  with_accel: boolean
  rank_key: string
  top_x: number
  auto_idle_days: number
  exit_need: number
  enter_th: number
  pool_n: number
  min_rps?: number | null
  initial_capital: number
  suggest_pct: number
  auto_index: string[]
  auto_boards: string[]
  t_mode: string
  max_holdings: number
  fee_commission_rate: number
  fee_commission_min: number
  fee_stamp_tax: number
  fee_handling_fee: number
  fee_regulatory_fee: number
  fee_transfer_fee: number
}

export interface LivePremarketResult {
  as_of: string
  health: number | null
  gate_state: number
  gate_changed: boolean
  rebalanced: boolean
  pool: Array<{ code: string; name?: string }>
  positions: number
  idle_days: number
  /** 数据滞后检测：数据截止日距今天 >4 个自然日 */
  stale: boolean
  stale_days: number
  signals: Array<{
    id: number
    code: string
    stype: string
    name: string
    reason: string
    suggest_amount: number
    ref_price: number | null
  }>
  warns: Array<{ id: number; code: string; stype: string; name: string; reason: string }>
  message: string
  pushed: boolean
}

export interface LiveSummary {
  pool: LivePoolState
  positions: LivePosition[]
  signals: LiveSignalItem[]
  fills: LiveFill[]
  feishu_configured: boolean
  config: LiveConfig
}

// ---- 盘中信号机（M2）----
export interface IntradaySignalOut {
  id: number
  code: string
  stype: string
  name: string
  reason: string
  suggest_amount: number | null
  ref_price: number
  bar: string
}

export interface IntradayRunResult {
  as_of: string
  signals: IntradaySignalOut[]
  suspended: Array<{ code: string; reason: string }>
  no_data: string[]
  fed_bars: number
  equity: number
  cash: number
  message: string
  pushed: boolean
  skipped?: string
}

export interface IntradayCodeStatus {
  code: string
  name: string
  price: number | null
  prev_close: number | null
  held: boolean
  opened: boolean
  full: boolean
  adds_done: number
  exit_stage: number
  last_bar: string | null
  in_pool: boolean
}

export interface IntradayStatus {
  session: boolean
  as_of: string | null
  gate_state: number
  codes: IntradayCodeStatus[]
  heartbeat: { ok_ts?: string; alerted?: boolean }
  t_mode: string
}

// ---- 滑点统计 / 影子运行（M3）----
export interface SlippageRow {
  fill_id: number
  signal_id: number | null
  code: string
  name: string
  stype: string
  side: 'buy' | 'sell'
  ref_price: number
  fill_price: number
  fill_volume: number
  slip_pct: number
  fill_time: string | null
}

export interface SlippageResult {
  rows: SlippageRow[]
  summary: {
    n: number
    avg_slip_pct: number | null
    buy_avg_slip_pct: number | null
    sell_avg_slip_pct: number | null
    worst_slip_pct: number | null
  }
}

export interface ShadowStats {
  n_signals: number
  n_filled: number
  n_ignored: number
  fill_rate: number | null
  shadow_pnl: number
  actual_pnl: number
  gap_pnl: number
  days: number
}

// ---- M4 就绪检查 ----
export interface ReadinessItem {
  key: string
  label: string
  ok: boolean
  detail: string
}

export interface ReadinessResult {
  ready: boolean
  items: ReadinessItem[]
}

// ---- 做T重构（T_REFACTOR） ----
export interface TOpenDebt {
  code: string
  name: string
  sell_date: string | null
  remaining: number
  sell_px_avg: number
  last_price: number
  float_pnl: number
}

export interface TRejectEvent {
  code: string
  name: string
  date: string
  /** chase=超追回上限 / discipline=回补限价未到 */
  type: string
  buy_price: number
  sell_px_avg: number
  reason: string
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
  volume: number
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
/** 实验矩阵：clock=趋势时钟×T 2×2（momentum_t）/ t_mode=做T四机制竞争（momentum_t+momentum_slot）/
 *  fwd_t_debt=正向T×债务时限 2×2（momentum_slot） */
export type ExperimentMatrix = 'clock' | 't_mode' | 'fwd_t_debt'

export interface ExperimentCreateRequest {
  name: string
  base_config: BacktestCreateRequest
  cells: ExperimentCell[]
  capitals: number[]
  start_date: string
  end_date: string
  /** 附带 E 格（纯日线 15 年参考，默认关） */
  with_e?: boolean
  /** 矩阵类型，默认 clock */
  matrix?: ExperimentMatrix
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
    // t_mode 矩阵：4 机制差值分解
    grid?: number | null
    discipline?: number | null
    off?: number | null
    time?: number | null
    discipline_vs_grid?: number | null
    grid_vs_off?: number | null
    time_vs_off?: number | null
    time_vs_grid?: number | null
  }>
  // t_mode 矩阵：total_return 主归因（4 机制两两差值）
  discipline_vs_grid?: number | null
  grid_vs_off?: number | null
  time_vs_off?: number | null
  time_vs_grid?: number | null
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
  /** 矩阵类型（clock/t_mode），历史数据缺省为 clock */
  matrix_type?: ExperimentMatrix
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
  matrix_type?: ExperimentMatrix
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

export interface DataIndexStatus {
  rows: number
  stocks: number
  snapshot_date?: string | null
  updated_at?: string | null
}

export interface DataIndexDailyStatus {
  rows: number
  indexes: number
  start: string | null
  end: string | null
  updated_at?: string | null
}

export interface DataIndustryStatus {
  rows: number
  stocks: number
  l3_count: number
  snapshot_date?: string | null
  updated_at?: string | null
}

export interface DataStockBasicStatus {
  total: number
  st_count: number
  delisted_count: number
  updated_at?: string | null
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
  /** 指数成分快照（未更新时为 null） */
  index: DataIndexStatus | null
  /** 基准指数日线（000905/000300，未更新时为 null） */
  index_daily?: DataIndexDailyStatus | null
  /** 申万行业快照（未更新时为 null） */
  industry: DataIndustryStatus | null
  /** 股票列表（ST/退市标记，未更新时为 null） */
  stock_basic: DataStockBasicStatus | null
  sources: DataSourceHealth[]
}

/** baostock API 调用监控（数据管理） */
export interface BsMonitor {
  ip: string
  today_count: number
  cap: number
  concurrency: number
  blacklisted: boolean
  freeze_count: number
  release_at: string | null
  last_check: string | null
  hint?: string
}

export interface BsCheckResult {
  ok: boolean
  monitor: BsMonitor
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
  timeout: number | null // 请求超时秒（空=用全局默认）
  max_tokens: number | null // 单次输出最大 token（空=用全局默认）
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
  timeout?: number | null
  max_tokens?: number | null
}

export interface KeyUpdateRequest {
  provider?: string
  api_key?: string
  model?: string | null
  base_url?: string | null
  label?: string
  sort_order?: number
  enabled?: boolean
  timeout?: number | null
  max_tokens?: number | null
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
