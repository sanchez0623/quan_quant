import { Card, Col, Empty, Row, Statistic } from 'antd'
import type { BacktestReport } from '../../api/types'
import { fmtMoney, fmtNum, fmtPct, pnlColor } from '../../utils/format'
import HeatmapChart from '../../components/HeatmapChart'

interface Props {
  report: BacktestReport
}

export default function MetricsTab({ report }: Props) {
  const m = report.metrics

  const mainCards: Array<{ title: string; value: string; color?: string }> = [
    { title: '总收益率', value: fmtPct(m.total_return), color: pnlColor(m.total_return) },
    { title: '年化收益', value: fmtPct(m.annual_return), color: pnlColor(m.annual_return) },
    { title: '最大回撤', value: fmtPct(m.max_drawdown), color: pnlColor(m.max_drawdown) },
    { title: '夏普比率', value: fmtNum(m.sharpe) },
    { title: '索提诺比率', value: fmtNum(m.sortino) },
    { title: '卡玛比率', value: fmtNum(m.calmar) },
    { title: '胜率', value: fmtPct(m.win_rate) },
    { title: '盈亏比', value: fmtNum(m.profit_loss_ratio) },
    { title: '总交易数', value: String(m.total_trades ?? 0) },
    { title: '总盈亏', value: fmtMoney(m.total_pnl), color: pnlColor(m.total_pnl) },
    { title: '总手续费', value: fmtMoney(m.commission_total) },
    { title: '期末权益', value: fmtMoney(m.end_equity) }
  ]

  const breakdownCards: Array<{ title: string; value: string; color?: string }> = [
    { title: 'T交易数', value: String(m.t_trade_count ?? 0) },
    { title: 'T胜率', value: fmtPct(m.t_win_rate) },
    { title: 'T盈亏', value: fmtMoney(m.t_pnl), color: pnlColor(m.t_pnl) },
    { title: '平均持仓天数', value: fmtNum(m.avg_hold_days, 1) },
    { title: '开仓盈亏', value: fmtMoney(m.open_pnl), color: pnlColor(m.open_pnl) },
    { title: '加仓盈亏', value: fmtMoney(m.add_pnl), color: pnlColor(m.add_pnl) },
    { title: '减仓盈亏', value: fmtMoney(m.reduce_pnl), color: pnlColor(m.reduce_pnl) },
    { title: '止损盈亏', value: fmtMoney(m.stop_loss_pnl), color: pnlColor(m.stop_loss_pnl) }
  ]

  return (
    <div>
      <Row gutter={[12, 12]}>
        {mainCards.map((c) => (
          <Col span={6} key={c.title}>
            <Card size="small">
              <Statistic title={c.title} value={c.value} valueStyle={{ fontSize: 20, color: c.color }} />
            </Card>
          </Col>
        ))}
      </Row>

      <Card size="small" title="做T与加减仓贡献分解" style={{ marginTop: 16 }}>
        <Row gutter={[12, 12]}>
          {breakdownCards.map((c) => (
            <Col span={6} key={c.title}>
              <Statistic title={c.title} value={c.value} valueStyle={{ fontSize: 18, color: c.color }} />
            </Col>
          ))}
        </Row>
      </Card>

      <Card size="small" title="月度收益热力图" style={{ marginTop: 16 }}>
        {report.monthly_returns && report.monthly_returns.length > 0 ? (
          <HeatmapChart data={report.monthly_returns} />
        ) : (
          <Empty description="暂无月度收益数据" />
        )}
      </Card>
    </div>
  )
}
