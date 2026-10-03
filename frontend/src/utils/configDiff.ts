import type { BacktestCreateRequest } from '../api/types'

/**
 * 顶层字段 -> 中文标签（模板对比 / 多回测对比共用）。
 * 新增回测顶层字段时在此登记，否则对比视图会静默漏掉该字段的差异
 * （历史事故：pool_refill_min 未登记 -> 换血线差异看不见）。
 */
const TOP_LABELS: Array<[string, string]> = [
  ['strategy_id', '策略'], ['period', '周期'], ['universe_auto', '动态选股'],
  ['start_date', '开始日期'], ['end_date', '结束日期'], ['end_date_today', '结束日=今天'],
  ['initial_capital', '初始资金'], ['warmup_days', '指标预热'], ['benchmark', '基准指数'],
  ['exclude_st', '剔除ST'],
  // ---- 交易成本 ----
  ['slippage_pct', '滑点'], ['commission_rate', '佣金费率'], ['commission_min', '最低佣金'],
  ['stamp_tax', '印花税'], ['transfer_fee', '过户费'], ['handling_fee', '经手费'],
  ['regulatory_fee', '证管费'],
  // ---- 月度出金 / 总资金止盈 ----
  ['monthly_withdraw_base', '月提取额'], ['t_profit_withdraw_pct', 'T盈利提成'],
  ['min_t_amount', '最小T金额'], ['nav_take_profit_pct', '总资金止盈'],
  ['nav_take_profit_withdraw_pct', '止盈提取收益'],
  // ---- 动态选股 ----
  ['auto_idle_days', '空仓触发'], ['auto_top_x', '池子大小'], ['auto_above_ma', '均线锚'],
  ['auto_with_accel', '加速项'], ['auto_min_rps', 'RPS下限'], ['auto_index', '候选域-指数'],
  ['auto_boards', '候选域-板块'], ['auto_rank_key', '排序键'], ['pool_refill_min', '枯竭换血线'],
  // ---- 趋势闸门 ----
  ['pool_gate', '池级趋势开关'], ['pool_gate_enter_th', '趋势触发阈值'],
  ['index_gate', '大盘趋势闸门'], ['index_gate_ma', '大盘闸门MA']
]

/** 回测配置 diff：把 params/risk_config/顶层标量拍平为 `group\u0000key` -> value 映射 */
export function flattenBacktestConfig(
  cfg: BacktestCreateRequest
): Map<string, { group: string; value: unknown }> {
  const map = new Map<string, { group: string; value: unknown }>()
  const put = (group: string, key: string, value: unknown) => {
    if (value === undefined || value === null) return
    map.set(`${group}\u0000${key}`, { group, value })
  }
  Object.entries(cfg.params ?? {}).forEach(([k, v]) => put('策略参数', k, v))
  Object.entries(cfg.risk_config ?? {}).forEach(([k, v]) => put('风控', k, v))
  if (Array.isArray(cfg.universe)) put('基础', '股票池', cfg.universe.join(', '))
  const rcfg = cfg as unknown as Record<string, unknown>
  TOP_LABELS.forEach(([k, label]) => {
    const v = rcfg[k]
    if (v !== undefined) put('基础', label, v)
  })
  return map
}

/** 配置值 -> 展示字符串（对比视图两侧共用，保证"相同"判定与展示一致） */
export function fmtDiffVal(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') return String(Math.round(v * 100) / 100)
  if (Array.isArray(v)) return v.length ? v.join(', ') : '（空）'
  return String(v)
}
