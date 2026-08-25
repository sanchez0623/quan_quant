import { Col, Form, InputNumber, Row, Select } from 'antd'

export const RISK_FIELDS: Array<{ key: string; label: string; hint: string }> = [
  { key: 'max_position_pct_per_stock', label: '个股仓位上限（%）', hint: '单只股票最大仓位占总资金比例' },
  { key: 'max_total_position_pct', label: '总仓位上限（%）', hint: '组合整体最大仓位比例' },
  { key: 'stop_loss_mode', label: '止损模式', hint: 'fixed 固定比例 / atr 动态 / trailing 移动止损' },
  { key: 'stop_loss_pct', label: '止损（%）', hint: '固定止损幅度' },
  { key: 'atr_period', label: 'ATR周期', hint: 'ATR 止损模式下的计算周期' },
  { key: 'atr_multiplier', label: 'ATR倍数', hint: 'ATR 止损模式下的倍数' },
  { key: 'take_profit_pct', label: '止盈（%）', hint: '0 表示不启用' },
  { key: 'trailing_stop_pct', label: '移动止损（%）', hint: '0 表示不启用' },
  { key: 'max_drawdown_breaker', label: '最大回撤熔断（%）', hint: '组合回撤达到该值后停止交易' },
  { key: 'max_intraday_trades', label: '日内交易次数上限', hint: '单日最大交易次数' }
]

export const DEFAULT_RISK_CONFIG: Record<string, string | number> = {
  max_position_pct_per_stock: 30,
  max_total_position_pct: 100,
  stop_loss_mode: 'fixed',
  stop_loss_pct: 8,
  atr_period: 14,
  atr_multiplier: 2,
  take_profit_pct: 0,
  trailing_stop_pct: 0,
  max_drawdown_breaker: 30,
  max_intraday_trades: 4
}

/** 风控配置表单（挂在 ['risk_config', key] 上） */
export default function RiskConfigForm() {
  return (
    <Row gutter={[16, 4]}>
      {RISK_FIELDS.map((f) => (
        <Col span={6} key={f.key}>
          <Form.Item
            name={['risk_config', f.key]}
            label={f.label}
            extra={f.hint}
          >
            {f.key === 'stop_loss_mode' ? (
              <Select
                options={[
                  { value: 'fixed', label: 'fixed（固定比例）' },
                  { value: 'atr', label: 'atr（ATR动态）' },
                  { value: 'trailing', label: 'trailing（移动止损）' }
                ]}
              />
            ) : (
              <InputNumber
                style={{ width: '100%' }}
                min={0}
                step={f.key === 'atr_multiplier' ? 0.5 : 1}
              />
            )}
          </Form.Item>
        </Col>
      ))}
    </Row>
  )
}
