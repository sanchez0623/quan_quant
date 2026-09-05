import { useMemo } from 'react'
import { Alert, Col, Collapse, Form, InputNumber, Row, Select, Tag, Tooltip, Typography } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'

type FieldType = 'number' | 'select'

export interface RiskField {
  key: string
  label: string
  hint: string
  type?: FieldType
  options?: Array<{ value: string | number; label: string }>
  precision?: number
  step?: number
  /** 分组：仓位与资金 / 止损 / 自适应 / 止盈与熔断 */
  group: string
  /** 条件显示：{ 依赖字段key: 允许值 }，全部满足才显示 */
  show_if?: Record<string, (string | number)[]>
}

export const RISK_FIELDS: RiskField[] = [
  // ---- 仓位与资金（与止损模式无关，常驻）----
  {
    key: 'max_position_pct_per_stock',
    label: '个股仓位上限（%）',
    hint: '单只股票最大仓位占总资金比例',
    group: '仓位与资金'
  },
  {
    key: 'max_total_position_pct',
    label: '总仓位上限（%）',
    hint: '组合整体最大仓位比例',
    group: '仓位与资金'
  },
  {
    key: 'max_holdings',
    label: '最大持仓只数',
    hint: '0 表示不限；只限制新开仓，可少于该数',
    group: '仓位与资金'
  },
  {
    key: 'cash_reserve_pct',
    label: '现金缓冲（%）',
    hint: '永不进场的资金比例，用于月度出金兜底',
    step: 0.5,
    precision: 1,
    group: '仓位与资金'
  },
  // ---- 组合层：板块集中度上限（只限开仓/加仓，不主动卖出；做T还债不受限）----
  {
    key: 'max_sector_pct',
    label: '单板块集中度上限（%）',
    hint: '同一板块（主板/创业板/科创板/北交所）持仓市值占净值上限；0=不启用',
    step: 5,
    precision: 0,
    group: '仓位与资金'
  },
  // ---- 止损：随 stop_loss_mode 切换 ----
  {
    key: 'stop_loss_mode',
    label: '止损模式',
    hint: '决定下方出现哪些止损参数',
    type: 'select',
    group: '止损',
    options: [
      { value: 'fixed', label: 'fixed（固定比例）' },
      { value: 'atr', label: 'atr（ATR动态）' },
      { value: 'trailing', label: 'trailing（移动止损）' },
      { value: 'atr_trailing', label: 'atr_trailing（ATR移动，推荐）' }
    ]
  },
  {
    key: 'stop_loss_pct',
    label: '止损（%）',
    hint: '跌破 成本×(1−止损%) 离场',
    group: '止损',
    show_if: { stop_loss_mode: ['fixed'] }
  },
  {
    key: 'atr_period',
    label: 'ATR周期',
    hint: '计算 ATR 的回看周期',
    group: '止损',
    show_if: { stop_loss_mode: ['atr', 'atr_trailing'] }
  },
  {
    key: 'atr_multiplier',
    label: '硬止损倍数 k1',
    hint: '成本项倍数：价格 ≤ 成本基准 − k1×ATR 即止损',
    step: 0.5,
    precision: 1,
    group: '止损',
    show_if: { stop_loss_mode: ['atr', 'atr_trailing'] }
  },
  {
    key: 'trailing_stop_pct',
    label: '移动止损（%）',
    hint: '跌破 持仓最高价×(1−回撤%) 离场，0 表示不启用',
    group: '止损',
    show_if: { stop_loss_mode: ['trailing'] }
  },
  {
    key: 'atr_trail_mult',
    label: '移动锁盈倍数 k2',
    hint: '相对持仓最高价的回撤倍数；5~12 区间稳健，≤3 偏紧，不建议继续微调',
    step: 0.5,
    precision: 1,
    group: '止损',
    show_if: { stop_loss_mode: ['atr_trailing'] }
  },
  {
    key: 'atr_cost_base',
    label: '成本基准',
    hint: 'first 用首笔开仓价，避免加仓抬高止损线',
    type: 'select',
    group: '止损',
    show_if: { stop_loss_mode: ['atr_trailing'] },
    options: [
      { value: 'first', label: 'first（首笔开仓价）' },
      { value: 'wavg', label: 'wavg（加权平均成本）' }
    ]
  },
  {
    key: 'atr_trail_floor',
    label: '止损线棘轮',
    hint: '开启后止损线只上不下',
    type: 'select',
    group: '止损',
    show_if: { stop_loss_mode: ['atr_trailing'] },
    options: [
      { value: 1, label: '开（只上不下）' },
      { value: 0, label: '关（可回落）' }
    ]
  },
  // ---- 双层止损（方案B）：做T仓独立档，核心仓沿用默认档 ----
  {
    key: 'trade_tier_on',
    label: '双层止损(做T仓)',
    hint: '开启后：核心仓(开仓/加仓)用默认止损档，做T仓用下方独立档（成本底线+独立ATR）',
    type: 'select',
    group: '止损',
    options: [
      { value: 1, label: '开（做T仓独立档）' },
      { value: 0, label: '关（沿用单一止损）' }
    ]
  },
  {
    key: 'trade_stop_pct',
    label: 'T仓成本底线（%）',
    hint: '做T仓价格 ≤ 成本×(1−底线%) 即止损（做T仓不参与固定止盈，靠网格高抛）',
    group: '止损',
    show_if: { trade_tier_on: [1] }
  },
  {
    key: 'trade_atr_mult',
    label: 'T仓硬止损倍数 k1',
    hint: '做T仓止损线 = max(成本−k1×ATR, 最高价−k2×ATR) 的 k1',
    step: 0.5,
    precision: 1,
    group: '止损',
    show_if: { trade_tier_on: [1] }
  },
  {
    key: 'trade_trail_mult',
    label: 'T仓移动倍数 k2',
    hint: '做T仓止损线 = max(成本−k1×ATR, 最高价−k2×ATR) 的 k2（越小越紧锁盈）',
    step: 0.5,
    precision: 1,
    group: '止损',
    show_if: { trade_tier_on: [1] }
  },
  // ---- 方案E：市况条件化保护 ----
  {
    key: 'regime_b_on',
    label: 'B仅趋势市启用',
    hint: '开启后：双层止损(做T仓独立档)只在趋势市生效，震荡/下跌市做T仓退回默认档（规避低波动市拖累）',
    type: 'select',
    group: '止损',
    show_if: { trade_tier_on: [1] },
    options: [
      { value: 1, label: '开（仅趋势市启用B档）' },
      { value: 0, label: '关（全程启用B档）' }
    ]
  },
  // ---- 自适应止损：仅 atr_trailing 生效（k1/k2 按市况缩放）----
  {
    key: 'adaptive',
    label: '自适应止损',
    hint: '按市场状态自动缩放 k1/k2：趋势确立放宽让利润奔跑，趋势破坏收紧',
    type: 'select',
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'] },
    options: [
      { value: 'off', label: 'off（关闭）' },
      { value: 'trend', label: 'trend（个股趋势）' },
      { value: 'vol', label: 'vol（波动率分位）' }
    ]
  },
  {
    key: 'adaptive_k_loose',
    label: '趋势市放宽倍数',
    hint: '趋势确立（trend）或高波动分位（vol）时 k1/k2 的放大倍数',
    step: 0.1,
    precision: 1,
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['trend', 'vol'] }
  },
  {
    key: 'adaptive_k_tight',
    label: '破位收紧倍数',
    hint: '跌破均线（trend）或低波动分位（vol）时 k1/k2 的缩小倍数',
    step: 0.1,
    precision: 1,
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['trend', 'vol'] }
  },
  {
    key: 'adaptive_trend_ma',
    label: '趋势均线周期',
    hint: 'trend 模式：收盘价与该均线的关系判定趋势是否确立',
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['trend'] }
  },
  {
    key: 'adaptive_slope_n',
    label: '均线斜率窗口',
    hint: 'trend 模式：均线 N 日斜率 ≥ 0 才算趋势确立',
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['trend'] }
  },
  {
    key: 'adaptive_vol_n',
    label: '波动分位窗口',
    hint: 'vol 模式：ATR% 滚动分位的回看窗口',
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['vol'] }
  },
  {
    key: 'adaptive_vol_hi',
    label: '高波分位阈值',
    hint: 'vol 模式：ATR% 分位高于此值 -> 放宽止损',
    step: 0.05,
    precision: 2,
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['vol'] }
  },
  {
    key: 'adaptive_vol_lo',
    label: '低波分位阈值',
    hint: 'vol 模式：ATR% 分位低于此值 -> 收紧止损',
    step: 0.05,
    precision: 2,
    group: '自适应止损',
    show_if: { stop_loss_mode: ['atr_trailing'], adaptive: ['vol'] }
  },
  // ---- 止盈与熔断（常驻）----
  {
    key: 'take_profit_pct',
    label: '止盈（%）',
    hint: '涨幅达到 成本×(1+止盈%) 离场；0 表示不启用',
    group: '止盈与熔断'
  },
  {
    key: 'max_drawdown_breaker',
    label: '最大回撤熔断（%）',
    hint: '组合回撤达到该值后停止开新仓',
    group: '止盈与熔断'
  },
  {
    key: 'max_intraday_trades',
    label: '日内交易次数上限',
    hint: '留空自动对齐策略 max_t_times；按需收紧',
    group: '止盈与熔断'
  }
]

export const DEFAULT_RISK_CONFIG: Record<string, string | number> = {
  max_position_pct_per_stock: 40,
  max_total_position_pct: 100,
  max_holdings: 3,
  cash_reserve_pct: 1.5,
  max_sector_pct: 0,
  stop_loss_mode: 'atr_trailing',
  stop_loss_pct: 12,
  atr_period: 14,
  atr_multiplier: 2.5,
  take_profit_pct: 40,
  trailing_stop_pct: 5,
  atr_trail_mult: 6,
  atr_cost_base: 'first',
  atr_trail_floor: 1,
  adaptive: 'trend',
  adaptive_k_loose: 1.5,
  adaptive_k_tight: 0.7,
  adaptive_trend_ma: 60,
  adaptive_slope_n: 5,
  adaptive_vol_n: 120,
  adaptive_vol_hi: 0.7,
  adaptive_vol_lo: 0.3,
  max_drawdown_breaker: 30,
  trade_tier_on: 0,
  trade_stop_pct: 10,
  trade_atr_mult: 3,
  trade_trail_mult: 5,
  regime_b_on: 0
}

/** 分组顺序（未列出的组排在末尾） */
const GROUP_ORDER = ['仓位与资金', '止损', '自适应止损', '止盈与熔断']

function matchesShowIf(f: RiskField, values: Record<string, unknown> | undefined): boolean {
  const cond = f.show_if
  if (!cond) return true
  return Object.entries(cond).every(([dep, allow]) => {
    const v = values?.[dep]
    if (v === undefined || v === null || v === '') return true
    return (allow ?? []).map(String).includes(String(v))
  })
}

/** 按当前止损模式生成一行人话规则说明 */
function describeStopRule(v: Record<string, unknown> | undefined): string {
  const mode = String(v?.stop_loss_mode ?? 'atr_trailing')
  const pct = v?.stop_loss_pct
  const n = v?.atr_period
  const k1 = v?.atr_multiplier
  const k2 = v?.atr_trail_mult
  const base = v?.atr_cost_base
  const floor = v?.atr_trail_floor
  const adaptive = String(v?.adaptive ?? 'trend')
  const suffix =
    adaptive === 'off'
      ? ''
      : `；自适应 ${adaptive === 'trend' ? '个股趋势' : '波动率分位'}：放宽 ×${v?.adaptive_k_loose ?? '-'} / 收紧 ×${v?.adaptive_k_tight ?? '-'}`
  switch (mode) {
    case 'fixed':
      return `固定止损：价格 ≤ 成本 ×（1 − ${pct ?? '-'}%）→ 清仓${suffix}`
    case 'atr':
      return `ATR 动态：价格 ≤ 成本 − ${k1 ?? '-'} × ATR(${n ?? '-'}) → 清仓（波动大自动放宽，波动小自动收紧）`
    case 'trailing':
      return `移动止损：价格 ≤ 持仓最高价 ×（1 − ${v?.trailing_stop_pct ?? '-'}%）→ 清仓`
    case 'atr_trailing':
      return (
        `ATR 移动：止损线 = max( ${base === 'wavg' ? '加权成本' : '首笔开仓价'} − ${k1 ?? '-'}×ATR(${n ?? '-'})，` +
        `最高价 − ${k2 ?? '-'}×ATR(${n ?? '-'}) )，` +
        `${floor === 0 || floor === false ? '可随最高价回落' : '棘轮只上不下'}${suffix}`
      )
    default:
      return '未启用止损'
  }
}

/** 风控配置表单（挂在 ['risk_config', key] 上） */
export default function RiskConfigForm() {
  const form = Form.useFormInstance()
  const values = Form.useWatch('risk_config', form) as Record<string, unknown> | undefined

  const groups = useMemo(() => {
    const map = new Map<string, RiskField[]>()
    for (const f of RISK_FIELDS) {
      if (!matchesShowIf(f, values)) continue
      if (!map.has(f.group)) map.set(f.group, [])
      map.get(f.group)!.push(f)
    }
    const ordered = [...map.keys()].sort((a, b) => {
      const ia = GROUP_ORDER.indexOf(a)
      const ib = GROUP_ORDER.indexOf(b)
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
    })
    return ordered.map((name) => ({ name, fields: map.get(name)! }))
  }, [values])

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="当前止损规则"
        description={describeStopRule(values)}
      />
      <Collapse
        size="small"
        defaultActiveKey={GROUP_ORDER}
        items={groups.map((g) => ({
          key: g.name,
          label: (
            <span>
              <b>{g.name}</b>
              <Tag style={{ marginLeft: 8 }}>{g.fields.length} 项</Tag>
            </span>
          ),
          children: (
            <Row gutter={[16, 0]}>
              {g.fields.map((f) => (
                <Col span={6} key={f.key}>
                  <Form.Item
                    name={['risk_config', f.key]}
                    label={
                      <span>
                        {f.label}
                        <Tooltip title={f.hint}>
                          <QuestionCircleOutlined style={{ marginLeft: 4, color: '#8c8c8c' }} />
                        </Tooltip>
                      </span>
                    }
                  >
                    {f.type === 'select' ? (
                      <Select options={f.options} />
                    ) : (
                      <InputNumber
                        style={{ width: '100%' }}
                        min={0}
                        step={f.step ?? 1}
                        precision={f.precision ?? 0}
                      />
                    )}
                  </Form.Item>
                </Col>
              ))}
            </Row>
          )
        }))}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        切换「止损模式」后只保留与之相关的参数；未显示的字段按后端默认值参与回测。
      </Typography.Text>
    </>
  )
}
