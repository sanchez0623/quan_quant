import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Card, Collapse, Descriptions, Progress, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { errDetail, getExperimentDetail } from '../api/client'
import type {
  ExperimentCell,
  ExperimentCellAttribution,
  ExperimentDetail,
  ExperimentMatrix,
  ExperimentMatrixItem
} from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import { fmtNum, fmtPct, pnlColor } from '../utils/format'

const CELL_ORDER: ExperimentCell[] = ['A', 'B', 'C', 'D']
const CELL_TITLE: Record<ExperimentCell, string> = {
  A: '日线×T',
  B: '盘中×T',
  C: '日线×无T',
  D: '盘中×无T',
  E: '纯日线15年(参考)'
}
/** t_mode 机制矩阵的格子标题（T_REFACTOR L3） */
const TMODE_CELL_TITLE: Record<ExperimentCell, string> = {
  A: '网格+双止损(L1)',
  B: '回补纪律(L2)',
  C: '无T基线',
  D: '时点规律T',
  E: '纯日线15年(参考)'
}

export default function ExperimentResult() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<ExperimentDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const fetch = useCallback(async () => {
    try {
      setDetail(await getExperimentDetail(id))
      setError('')
    } catch (err) {
      setError(errDetail(err, '加载实验失败'))
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    fetch()
  }, [fetch])

  useEffect(() => {
    if (!detail || detail.status === 'success' || detail.status === 'failed') return
    const timer = window.setInterval(fetch, 10_000)
    return () => window.clearInterval(timer)
  }, [detail, fetch])

  if (error) {
    return (
      <Card title="对比实验详情">
        <Typography.Text type="danger">{error}</Typography.Text>
      </Card>
    )
  }
  if (!detail || loading) {
    return (
      <Card title="对比实验详情">
        <Typography.Text type="secondary">加载中...</Typography.Text>
      </Card>
    )
  }

  const matrixType: ExperimentMatrix = detail.matrix_type ?? 'clock'
  const isTmode = matrixType === 't_mode'
  const cellTitle = (c: ExperimentCell) => (isTmode ? TMODE_CELL_TITLE[c] : CELL_TITLE[c])

  const byKey = (cell: ExperimentCell, capital: number) =>
    detail.matrix.find((m) => m.cell === cell && m.capital === capital)

  const capitalRows = detail.capitals.map((c) => ({ capital: c, key: String(c) }))

  const matrixColumns: ColumnsType<{ capital: number; key: string }> = [
    { title: '资金档', dataIndex: 'capital', width: 110, render: (v: number) => `${v / 10000}万` },
    ...CELL_ORDER.filter((c) => detail.cells.includes(c)).map((c) => ({
      title: cellTitle(c),
      key: c,
      width: 200,
      render: (_: unknown, row: { capital: number }) => {
        const m = byKey(c, row.capital)
        if (!m) return <Tag>—</Tag>
        return (
          <Space
            direction="vertical"
            size={2}
            style={{ cursor: 'pointer', width: '100%' }}
            onClick={() => navigate(`/backtests/${m.task_id}`)}
          >
            <TaskStatusTag status={m.status} />
            {m.status === 'success' && m.metrics && (
              <>
                <span style={{ color: pnlColor(m.metrics.total_return) }}>
                  收益 {fmtPct(m.metrics.total_return)}
                </span>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  夏普 {fmtNum(m.metrics.sharpe, 2)} · 回撤 {fmtPct(m.metrics.max_drawdown)}
                </Typography.Text>
              </>
            )}
            {m.status === 'failed' && (
              <Typography.Text type="danger" style={{ fontSize: 11 }} ellipsis={{ tooltip: m.error }}>
                {m.error}
              </Typography.Text>
            )}
            {m.status === 'running' && <Progress percent={Math.round(m.progress)} size="small" />}
          </Space>
        )
      }
    }))
  ]

  const attributionEntries = Object.entries(detail.attribution.per_capital ?? {})

  const METRIC_LABELS: Record<string, string> = {
    total_return: '总收益',
    sharpe: '夏普',
    max_drawdown: '最大回撤',
    t_pnl: 'T盈亏(元)',
    t_pnl_closed: 'T已闭环盈亏(元)',
    commission_total: '手续费(元)'
  }

  const fmtDelta = (metric: string, v?: number | null): string => {
    if (v === null || v === undefined || Number.isNaN(v)) return '-'
    if (metric === 'sharpe') return v.toFixed(2)
    if (metric === 't_pnl' || metric === 't_pnl_closed' || metric === 'commission_total')
      return Math.round(v).toLocaleString('zh-CN')
    return `${(v * 100).toFixed(2)}%`
  }

  const attrCards = attributionEntries.map(([cap, a]: [string, ExperimentCellAttribution]) => {
    const metricRows = Object.entries(a.metrics ?? {}).map(([k, v]) => ({ key: k, metric: k, ...v }))
    const metricColumns: ColumnsType<(typeof metricRows)[number]> = isTmode
      ? [
          {
            title: '指标',
            dataIndex: 'metric',
            width: 110,
            render: (k: string) => METRIC_LABELS[k] ?? k
          },
          ...(
            [
              ['discipline_vs_grid', '纪律−网格'],
              ['grid_vs_off', '网格−无T'],
              ['time_vs_off', '时点−无T'],
              ['time_vs_grid', '时点−网格']
            ] as const
          ).map(([dk, label]) => ({
            title: label,
            dataIndex: dk,
            align: 'right' as const,
            render: (v: number | null | undefined, r: (typeof metricRows)[number]) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          }))
        ]
      : [
          { title: '指标', dataIndex: 'metric', width: 110, render: (k: string) => METRIC_LABELS[k] ?? k },
          {
            title: 'T边际 A−C',
            dataIndex: 't_margin_ac',
            align: 'right',
            render: (v: number | null | undefined, r) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          },
          {
            title: 'T边际 B−D',
            dataIndex: 't_margin_bd',
            align: 'right',
            render: (v: number | null | undefined, r) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          },
          {
            title: '时钟 A−B',
            dataIndex: 'clock_ab',
            align: 'right',
            render: (v: number | null | undefined, r) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          },
          {
            title: '时钟 C−D',
            dataIndex: 'clock_cd',
            align: 'right',
            render: (v: number | null | undefined, r) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          },
          {
            title: '交互',
            dataIndex: 'interaction',
            align: 'right',
            render: (v: number | null | undefined, r) => (
              <span style={{ color: pnlColor(v) }}>{fmtDelta(r.metric, v)}</span>
            )
          }
        ]
    return (
      <Card key={cap} size="small" title={`资金档 ${Number(cap) / 10000}万 归因`} style={{ marginBottom: 16 }}>
        <Space size="large" wrap>
          {isTmode ? (
            <>
              <Statistic
                title="回补纪律 − 网格"
                value={a.discipline_vs_grid ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.discipline_vs_grid) }}
                suffix="%"
              />
              <Statistic
                title="网格 − 无T（T层净价值）"
                value={a.grid_vs_off ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.grid_vs_off) }}
                suffix="%"
              />
              <Statistic
                title="时点 − 无T"
                value={a.time_vs_off ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.time_vs_off) }}
                suffix="%"
              />
              <Statistic
                title="时点 − 网格"
                value={a.time_vs_grid ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.time_vs_grid) }}
                suffix="%"
              />
            </>
          ) : (
            <>
              <Statistic
                title="T 边际贡献 A−C"
                value={a.t_margin_ac ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.t_margin_ac) }}
                suffix="%"
              />
              <Statistic
                title="T 边际贡献 B−D"
                value={a.t_margin_bd ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.t_margin_bd) }}
                suffix="%"
              />
              <Statistic
                title="时钟效应 A−B"
                value={a.clock_ab ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.clock_ab) }}
                suffix="%"
              />
              <Statistic
                title="时钟效应 C−D"
                value={a.clock_cd ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.clock_cd) }}
                suffix="%"
              />
              <Statistic
                title="交互项 (A−C)−(B−D)"
                value={a.interaction ?? undefined}
                precision={2}
                valueStyle={{ color: pnlColor(a.interaction) }}
                suffix="%"
              />
            </>
          )}
        </Space>
        {!isTmode && (
          <div style={{ marginTop: 8 }}>
            {a.t_consistent === true && <Tag color="green">两列 T 估计同向 ✓</Tag>}
            {a.t_consistent === false && <Tag color="orange">两列 T 估计异向 ⚠（T×时钟交互强）</Tag>}
            {a.clock_consistent === true && <Tag color="green">两行时钟估计同向 ✓</Tag>}
            {a.clock_consistent === false && <Tag color="orange">两行时钟估计异向 ⚠</Tag>}
          </div>
        )}
        <Table
          size="small"
          pagination={false}
          style={{ marginTop: 12 }}
          rowKey="key"
          dataSource={metricRows}
          columns={metricColumns}
          title={() => '各指标差值分解'}
        />
      </Card>
    )
  })

  const eItems = detail.matrix.filter((m) => m.cell === 'E' && m.status === 'success' && m.metrics)

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title={`对比实验：${detail.name}`}
        extra={
          <Space>
            <TaskStatusTag status={detail.status} />
            <span>进度 {detail.progress}%</span>
          </Space>
        }
      >
        <Descriptions size="small" column={5}>
          <Descriptions.Item label="实验ID">{detail.experiment_id}</Descriptions.Item>
          <Descriptions.Item label="矩阵类型">
            <Tag color={isTmode ? 'geekblue' : 'default'}>
              {isTmode ? '做T四机制竞争' : '时钟×做T'}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="区间">
            {detail.start_date} ~ {detail.end_date}
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">{detail.created_at}</Descriptions.Item>
          <Descriptions.Item label="子任务">{detail.sub_task_ids.length} 个</Descriptions.Item>
        </Descriptions>
        {detail.error && <Typography.Text type="danger">{detail.error}</Typography.Text>}
      </Card>

      <Card title="矩阵总览（点击格子查看完整报告）">
        <Table
          size="small"
          pagination={false}
          rowKey="key"
          columns={matrixColumns}
          dataSource={capitalRows}
        />
      </Card>

      {eItems.length > 0 && (
        <Card size="small" title="E 格参考（纯日线 · 2010 起 · 仅趋势层，不进矩阵归因）">
          {eItems.map((m) => (
            <Descriptions key={m.task_id} size="small" column={4}>
              <Descriptions.Item label="资金档">{m.capital / 10000}万</Descriptions.Item>
              <Descriptions.Item label="年化">
                <span style={{ color: pnlColor(m.metrics?.annual_return) }}>
                  {fmtPct(m.metrics?.annual_return)}
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="总收益">
                <span style={{ color: pnlColor(m.metrics?.total_return) }}>
                  {fmtPct(m.metrics?.total_return)}
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="回撤">{fmtPct(m.metrics?.max_drawdown)}</Descriptions.Item>
              <Descriptions.Item label="夏普">{fmtNum(m.metrics?.sharpe, 2)}</Descriptions.Item>
              <Descriptions.Item label="交易数">{m.metrics?.total_trades}</Descriptions.Item>
              <Descriptions.Item label="任务">
                <a onClick={() => navigate(`/backtests/${m.task_id}`)}>{m.task_id}</a>
              </Descriptions.Item>
            </Descriptions>
          ))}
        </Card>
      )}

      <Card title={isTmode ? '机制竞争归因（总收益差值）' : '归因分解'}>
        {attrCards.length ? (
          <>
            {attrCards}
            <Typography.Paragraph style={{ marginTop: 8 }}>
              <b>结论：</b>
              {detail.attribution.decision}
            </Typography.Paragraph>
          </>
        ) : (
          <Typography.Text type="secondary">暂无已完成格子，等待子任务回测...（每 10s 自动刷新）</Typography.Text>
        )}
      </Card>

      <Collapse
        items={[
          {
            key: 'config',
            label: isTmode
              ? '基座配置摘要（各格只差 t_mode / initial_capital）'
              : '基座配置摘要（各格只差 trend_clock / max_t_times / initial_capital）',
            children: (
              <pre
                style={{
                  fontSize: 12,
                  background: '#f5f5f5',
                  padding: 12,
                  borderRadius: 6,
                  maxHeight: 400,
                  overflow: 'auto'
                }}
              >
                {JSON.stringify(detail.base_config, null, 2)}
              </pre>
            )
          }
        ]}
      />
    </Space>
  )
}
