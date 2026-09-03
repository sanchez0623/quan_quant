import { useMemo } from 'react'
import { Alert, Card, Col, Empty, Row, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type {
  BacktestReport,
  TOpenDebt,
  TRejectEvent,
  WithdrawalLogItem,
  WithdrawalSummary
} from '../../api/types'
import { fmtMoney, fmtNum, fmtPct, pnlColor } from '../../utils/format'
import HeatmapChart from '../../components/HeatmapChart'

const WITHDRAW_TYPE_LABEL: Record<WithdrawalLogItem['type'], string> = {
  t_profit: 'T盈利提成',
  month_topup: '月末兜底补齐',
  shortfall: '缺口（盈利不足，未提取）',
  shortfall_recover: '历史缺口追偿'
}

/** 月度出金明细表：每行一个月份，展开显示该月逐笔记录 */
function WithdrawalTable({ wd }: { wd: WithdrawalSummary }) {
  const rows = useMemo(() => {
    const byMonth = new Map<string, WithdrawalLogItem[]>()
    for (const it of wd.log ?? []) {
      if (!byMonth.has(it.month)) byMonth.set(it.month, [])
      byMonth.get(it.month)!.push(it)
    }
    return Object.keys(wd.months ?? {})
      .sort()
      .map((month) => {
        const items = byMonth.get(month) ?? []
        const sum = (t: WithdrawalLogItem['type']) =>
          items.filter((i) => i.type === t).reduce((s, i) => s + i.amount, 0)
        return { month, total: wd.months[month] ?? 0, t: sum('t_profit'), top: sum('month_topup'), gap: sum('shortfall'), items }
      })
  }, [wd])

  const columns: ColumnsType<(typeof rows)[number]> = [
    { title: '月份', dataIndex: 'month', width: 100 },
    {
      title: '出金总额',
      dataIndex: 'total',
      align: 'right',
      render: (v: number, r) => (
        <span style={{ color: v >= wd.monthly_base ? '#3f8600' : '#cf1322' }}>{fmtMoney(v)}</span>
      )
    },
    { title: 'T盈利提成', dataIndex: 't', align: 'right', render: (v: number) => fmtMoney(v) },
    { title: '月末补齐', dataIndex: 'top', align: 'right', render: (v: number) => fmtMoney(v) },
    {
      title: '缺口',
      dataIndex: 'gap',
      align: 'right',
      render: (v: number) => (v > 0 ? <span style={{ color: '#cf1322' }}>{fmtMoney(v)}</span> : '-')
    }
  ]

  const detailColumns: ColumnsType<WithdrawalLogItem> = [
    { title: '日期', dataIndex: 'date', width: 150 },
    { title: '类型', dataIndex: 'type', width: 220, render: (v: WithdrawalLogItem['type']) => WITHDRAW_TYPE_LABEL[v] ?? v },
    { title: '金额', dataIndex: 'amount', align: 'right', render: (v: number) => fmtMoney(v) }
  ]

  return (
    <Table<(typeof rows)[number]>
      size="small"
      rowKey="month"
      dataSource={rows}
      columns={columns}
      pagination={false}
      expandable={{
        expandedRowRender: (r) =>
          r.items.length > 0 ? (
            <Table<WithdrawalLogItem>
              size="small"
              rowKey={(it, idx) => `${it.date}-${idx}`}
              dataSource={r.items}
              columns={detailColumns}
              pagination={false}
            />
          ) : (
            <Typography.Text type="secondary">该月无逐笔出金记录</Typography.Text>
          )
      }}
    />
  )
}

interface Props {
  report: BacktestReport
}

export default function MetricsTab({ report }: Props) {
  const m = report.metrics

  const mainCards: Array<{ title: string; value: string; color?: string }> = [
    { title: '总收益率', value: fmtPct(m.total_return), color: pnlColor(m.total_return) },
    ...(m.benchmark_return != null
      ? [
          {
            title: `同期${report.benchmark?.name || '基准'}`,
            value: fmtPct(m.benchmark_return),
            color: pnlColor(m.benchmark_return)
          },
          ...(m.excess_return != null
            ? [{
                title: '超额收益',
                value: fmtPct(m.excess_return),
                color: pnlColor(m.excess_return)
              }]
            : [])
        ]
      : []),
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
    { title: 'T盈亏（配对口径）', value: fmtMoney(m.t_pnl), color: pnlColor(m.t_pnl) },
    {
      title: 'T已闭环盈亏',
      value: m.t_pnl_closed != null ? fmtMoney(m.t_pnl_closed) : '-',
      color: m.t_pnl_closed != null ? pnlColor(m.t_pnl_closed) : undefined
    },
    { title: 'T盈亏比', value: m.t_payoff != null ? fmtNum(m.t_payoff, 2) : '-' },
    { title: '平均持仓天数', value: fmtNum(m.avg_hold_days, 1) },
    { title: '开仓盈亏', value: fmtMoney(m.open_pnl), color: pnlColor(m.open_pnl) },
    { title: '加仓盈亏', value: fmtMoney(m.add_pnl), color: pnlColor(m.add_pnl) },
    { title: '减仓盈亏', value: fmtMoney(m.reduce_pnl), color: pnlColor(m.reduce_pnl) },
    { title: '止损盈亏', value: fmtMoney(m.stop_loss_pnl), color: pnlColor(m.stop_loss_pnl) }
  ]

  // ---- T_REFACTOR：期末未闭环债务 + 纪律审计 ----
  const openDebts = report.t_open_debts ?? []
  const rejectEvents = report.t_reject_events ?? []
  const debtColumns: ColumnsType<TOpenDebt> = [
    { title: '代码', dataIndex: 'code', width: 90 },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    { title: '卖出日', dataIndex: 'sell_date', width: 110, render: (v: string | null) => v ?? '-' },
    {
      title: '未回补股数',
      dataIndex: 'remaining',
      width: 110,
      align: 'right',
      render: (v: number) => v.toLocaleString('zh-CN')
    },
    { title: '卖出均价', dataIndex: 'sell_px_avg', width: 100, align: 'right', render: (v: number) => fmtNum(v, 3) },
    { title: '期末价', dataIndex: 'last_price', width: 90, align: 'right', render: (v: number) => fmtNum(v, 3) },
    {
      title: '浮亏计提',
      dataIndex: 'float_pnl',
      width: 120,
      align: 'right',
      render: (v: number) => <span style={{ color: pnlColor(v) }}>{fmtMoney(v)}</span>
    }
  ]
  const rejectColumns: ColumnsType<TRejectEvent> = [
    { title: '日期', dataIndex: 'date', width: 110 },
    { title: '代码', dataIndex: 'code', width: 90 },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '类型',
      dataIndex: 'type',
      width: 130,
      render: (v: string) =>
        v === 'chase' ? <Tag color="orange">超追回上限</Tag> : <Tag color="purple">回补限价未到</Tag>
    },
    { title: '现价', dataIndex: 'buy_price', width: 90, align: 'right', render: (v: number) => fmtNum(v, 3) },
    { title: '卖出均价', dataIndex: 'sell_px_avg', width: 100, align: 'right', render: (v: number) => fmtNum(v, 3) },
    { title: '原因', dataIndex: 'reason', ellipsis: true }
  ]

  const wd = report.withdrawal
  const hasWithdrawal = !!wd && (wd.monthly_base > 0 || wd.total > 0)
  const withdrawCards: Array<{ title: string; value: string; color?: string }> = [
    { title: '累计提取', value: fmtMoney(m.withdrawn_total ?? 0), color: pnlColor(m.withdrawn_total ?? 0) },
    { title: 'T盈利提成', value: fmtMoney(m.t_profit_withdrawn ?? 0) },
    { title: '月末补齐', value: fmtMoney(m.month_topup_withdrawn ?? 0) },
    {
      title: '总资金止盈提取',
      value: (m.nav_withdrawn ?? 0) > 0 || (m.nav_withdraw_times ?? 0) > 0
        ? `${fmtMoney(m.nav_withdrawn ?? 0)}（${m.nav_withdraw_times ?? 0}次）`
        : '-'
    },
    {
      title: '出金覆盖率',
      value: m.withdrawal_coverage != null ? fmtPct(m.withdrawal_coverage) : '-',
      color: m.withdrawal_coverage != null ? pnlColor(m.withdrawal_coverage) : undefined
    },
    {
      title: '未补缺口',
      value: m.shortfall_unrecovered != null ? fmtMoney(m.shortfall_unrecovered) : '-',
      color: m.shortfall_unrecovered != null ? '#cf1322' : undefined
    },
    {
      title: '已追偿缺口',
      value: m.shortfall_recovered != null ? fmtMoney(m.shortfall_recovered) : '-',
      color: m.shortfall_recovered != null ? '#3f8600' : undefined
    }
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

      {report.engine_version === 't_refactor_v1' && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 16 }}
          message="做T盈亏为配对口径：已闭环价差 + 期末未闭环浮亏计提（引擎 t_refactor_v1），与旧版结果不可直接比较。"
        />
      )}

      {openDebts.length > 0 && (
        <Card
          size="small"
          title={`期末未闭环做T债务（${openDebts.length} 只，浮亏已计提进 T盈亏）`}
          style={{ marginTop: 16 }}
        >
          <Table<TOpenDebt>
            size="small"
            rowKey="code"
            dataSource={openDebts}
            columns={debtColumns}
            pagination={false}
          />
        </Card>
      )}

      {rejectEvents.length > 0 && (
        <Card
          size="small"
          title={`做T纪律审计 · 追回/回补被拒（${rejectEvents.length} 次，不计入交易明细）`}
          style={{ marginTop: 16 }}
        >
          <Table<TRejectEvent>
            size="small"
            rowKey={(_, i) => String(i)}
            dataSource={rejectEvents}
            columns={rejectColumns}
            pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
          />
        </Card>
      )}

      {hasWithdrawal && wd && (
        <Card
          size="small"
          title="月度出金（落袋为安）"
          extra={`月度目标 ${fmtMoney(wd.monthly_base)} · 逐笔T提成 + 月末兜底 · 统计基于调整净值（出金不算亏损）`}
          style={{ marginTop: 16 }}
        >
          <Row gutter={[12, 12]}>
            {withdrawCards.map((c) => (
              <Col span={6} key={c.title}>
                <Statistic title={c.title} value={c.value} valueStyle={{ fontSize: 18, color: c.color }} />
              </Col>
            ))}
          </Row>
          {Object.keys(wd.months ?? {}).length > 0 && (
            <div style={{ marginTop: 12 }}>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                说明：目标为每月出金 {fmtMoney(wd.monthly_base)}。先按逐笔做T盈利提取 10% 落袋，
                月末若当月累计不足目标，从累计盈利中兜底补齐；受「不取本金」护栏限制，当月盈利不足时
                会出现提取不足或缺口（详见各月明细，点击月份展开）。当月达标且后续月份有盈余现金时，
                会优先追偿历史缺口（明细中「历史缺口追偿」记录）。
              </Typography.Paragraph>
              <WithdrawalTable wd={wd} />
            </div>
          )}
        </Card>
      )}

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
