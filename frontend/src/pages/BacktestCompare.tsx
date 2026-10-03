import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Button, Card, Empty, Radio, Space, Spin, Switch, Table, Tag, Tooltip, Typography } from 'antd'
import { ArrowLeftOutlined, CloseOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { errDetail, getBacktestReport, getStrategies } from '../api/client'
import type { BacktestReport, EquityPoint, Metrics, Strategy } from '../api/types'
import { RISK_FIELDS } from '../components/RiskConfigForm'
import EchartsReact from '../components/EchartsReact'
import { flattenBacktestConfig, fmtDiffVal } from '../utils/configDiff'
import { fmtMoney, fmtNum, fmtPct } from '../utils/format'

/** 同时对比上限（配色与横向可读性；与列表页 COMPARE_MAX 保持一致） */
const MAX_TASKS = 8
const COLORS = ['#1f4e79', '#cf1322', '#3f8600', '#fa8c16', '#722ed1', '#13c2c2', '#eb2f96', '#8c8c8c']

/** 曲线口径：归一化净值 / 调整净值（含累计出金）/ 原始权益 */
type CurveMode = 'norm' | 'adjusted' | 'raw'

interface Entry {
  id: string
  report?: BacktestReport
  error?: string
}

interface MetricDef {
  label: string
  key: string
  get: (m: Metrics) => number | null | undefined
  render: (v: number | null | undefined) => string
  /** 最优方向：high=越大越好，low=越小越好，null=不评判（次数类/口径类） */
  better: 'high' | 'low' | null
}

const METRIC_DEFS: MetricDef[] = [
  { label: '总收益', key: 'total_return', get: (m) => m.total_return, render: (v) => fmtPct(v), better: 'high' },
  { label: '年化收益', key: 'annual_return', get: (m) => m.annual_return, render: (v) => fmtPct(v), better: 'high' },
  { label: '基准收益', key: 'benchmark_return', get: (m) => m.benchmark_return, render: (v) => fmtPct(v), better: null },
  { label: '超额收益', key: 'excess_return', get: (m) => m.excess_return, render: (v) => fmtPct(v), better: 'high' },
  { label: '最大回撤', key: 'max_drawdown', get: (m) => m.max_drawdown, render: (v) => fmtPct(v), better: 'high' },
  { label: '夏普', key: 'sharpe', get: (m) => m.sharpe, render: (v) => fmtNum(v), better: 'high' },
  { label: '索提诺', key: 'sortino', get: (m) => m.sortino, render: (v) => fmtNum(v), better: 'high' },
  { label: '卡玛', key: 'calmar', get: (m) => m.calmar, render: (v) => fmtNum(v), better: 'high' },
  { label: '胜率', key: 'win_rate', get: (m) => m.win_rate, render: (v) => fmtPct(v), better: 'high' },
  { label: '盈亏比', key: 'profit_loss_ratio', get: (m) => m.profit_loss_ratio, render: (v) => fmtNum(v), better: 'high' },
  { label: '交易次数', key: 'total_trades', get: (m) => m.total_trades, render: (v) => (v == null ? '-' : String(v)), better: null },
  { label: '平均持仓天数', key: 'avg_hold_days', get: (m) => m.avg_hold_days, render: (v) => fmtNum(v), better: null },
  { label: '总盈亏', key: 'total_pnl', get: (m) => m.total_pnl, render: (v) => fmtMoney(v), better: 'high' },
  { label: '调整口径总盈亏', key: 'adj_pnl', get: (m) => m.adj_pnl, render: (v) => fmtMoney(v), better: 'high' },
  { label: '底仓方向收益', key: 'position_pnl', get: (m) => m.position_pnl, render: (v) => fmtMoney(v), better: 'high' },
  { label: '做T盈亏', key: 't_pnl', get: (m) => m.t_pnl, render: (v) => fmtMoney(v), better: 'high' },
  { label: '做T胜率', key: 't_win_rate', get: (m) => m.t_win_rate, render: (v) => fmtPct(v), better: 'high' },
  { label: '做T次数', key: 't_trade_count', get: (m) => m.t_trade_count, render: (v) => (v == null ? '-' : String(v)), better: null },
  { label: '累计出金', key: 'withdrawn_total', get: (m) => m.withdrawn_total, render: (v) => fmtMoney(v), better: 'high' },
  { label: '止盈提取累计', key: 'nav_withdrawn', get: (m) => m.nav_withdrawn, render: (v) => fmtMoney(v), better: 'high' },
  { label: '出金覆盖率', key: 'withdrawal_coverage', get: (m) => m.withdrawal_coverage, render: (v) => fmtPct(v), better: 'high' },
  { label: '总手续费', key: 'commission_total', get: (m) => m.commission_total, render: (v) => fmtMoney(v), better: 'low' },
  { label: '后半段年化衰减', key: 'oos_health.decay_pct', get: (m) => m.oos_health?.decay_pct, render: (v) => fmtPct(v), better: 'low' }
]

interface MetricCell {
  text: string
  flag: 'best' | 'worst' | null
}

interface MetricRow {
  key: string
  label: string
  cells: MetricCell[]
}

interface DiffRow {
  key: string
  group: string
  label: string
  values: string[]
  diff: boolean
}

/** 短标签：任务名过长时截断（列头空间有限） */
function shortName(name: string, id: string): string {
  const n = (name || '').trim() || id
  return n.length > 14 ? `${n.slice(0, 14)}…` : n
}

/** 取该任务在某口径下的曲线值 */
function curveValue(p: EquityPoint, mode: CurveMode): number {
  if (mode === 'raw') return p.equity
  if (mode === 'adjusted') return p.adjusted_equity ?? p.equity
  return p.equity
}

export default function BacktestCompare() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const ids = useMemo(
    () => (params.get('ids') || '').split(',').map((s) => s.trim()).filter(Boolean).slice(0, MAX_TASKS),
    [params]
  )
  const [entries, setEntries] = useState<Entry[]>([])
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState<CurveMode>('norm')
  const [onlyDiff, setOnlyDiff] = useState(true)
  const [strategies, setStrategies] = useState<Strategy[]>([])

  useEffect(() => {
    getStrategies().then(setStrategies).catch(() => setStrategies([]))
  }, [])

  useEffect(() => {
    let alive = true
    if (!ids.length) {
      setEntries([])
      setLoading(false)
      return
    }
    setLoading(true)
    Promise.all(
      ids.map(async (id): Promise<Entry> => {
        try {
          return { id, report: await getBacktestReport(id) }
        } catch (err) {
          return { id, error: errDetail(err, '报告加载失败') }
        }
      })
    ).then((res) => {
      if (!alive) return
      setEntries(res)
      setLoading(false)
    })
    return () => {
      alive = false
    }
  }, [ids])

  const loaded = useMemo(() => entries.filter((e): e is Entry & { report: BacktestReport } => !!e.report), [entries])
  const failed = useMemo(() => entries.filter((e) => e.error), [entries])
  const reports = useMemo(() => loaded.map((e) => e.report), [loaded])

  /** X 轴 = 各曲线日期并集（不同回测区间也能叠图，缺口用 connectNulls 连接） */
  const axis = useMemo(() => {
    const s = new Set<string>()
    reports.forEach((r) => r.equity_curve.forEach((p) => s.add(p.date)))
    return [...s].sort()
  }, [reports])

  const curves = useMemo(
    () =>
      reports.map((r) => {
        const byDate = new Map(r.equity_curve.map((p) => [p.date, p]))
        const raw = axis.map((d) => {
          const p = byDate.get(d)
          return p ? curveValue(p, mode) : null
        })
        if (mode === 'raw') return raw.map((v) => (v == null ? null : +v.toFixed(2)))
        const base = raw.find((v) => v != null) ?? null
        if (!base) return raw.map(() => null)
        return raw.map((v) => (v == null ? null : +(v / base).toFixed(6)))
      }),
    [reports, axis, mode]
  )

  /** 基准线：仅当所有任务用同一个基准指数时才叠加（否则多基准线无法解读） */
  const bench = useMemo(() => {
    if (!reports.length) return null
    const keys = new Set(reports.map((r) => r.benchmark?.index_key).filter(Boolean))
    if (keys.size !== 1) return null
    const first = reports.find((r) => r.benchmark?.curve?.length)
    if (!first?.benchmark) return null
    const byDate = new Map(first.benchmark.curve.map((p) => [p.date, p.equity]))
    const raw = axis.map((d) => byDate.get(d) ?? null)
    const base = raw.find((v) => v != null) ?? null
    if (!base) return null
    const data = mode === 'raw' ? raw.map((v) => (v == null ? null : +v.toFixed(2))) : raw.map((v) => (v == null ? null : +(v / base).toFixed(6)))
    return { name: `基准 ${first.benchmark.name || first.benchmark.index_key}`, data }
  }, [reports, axis, mode])

  const curveOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { type: 'scroll', data: [...loaded.map((e) => shortName(e.report.name, e.id)), ...(bench ? [bench.name] : [])] },
      grid: { left: 70, right: 24, top: 44, bottom: 56 },
      xAxis: { type: 'category', data: axis },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: {
          formatter: (v: number) => (mode === 'raw' ? `${(v / 10000).toFixed(0)}万` : v.toFixed(2))
        }
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 8, height: 18 }],
      series: [
        ...loaded.map((e, i) => ({
          name: shortName(e.report.name, e.id),
          type: 'line' as const,
          data: curves[i],
          showSymbol: false,
          connectNulls: true,
          lineStyle: { width: 2, color: COLORS[i % COLORS.length] },
          itemStyle: { color: COLORS[i % COLORS.length] }
        })),
        ...(bench
          ? [
              {
                name: bench.name,
                type: 'line' as const,
                data: bench.data,
                showSymbol: false,
                connectNulls: true,
                lineStyle: { type: 'dashed' as const, width: 1.5, color: '#8c8c8c' },
                itemStyle: { color: '#8c8c8c' }
              }
            ]
          : [])
      ]
    }),
    [loaded, curves, axis, bench, mode]
  )

  const drawdownOption = useMemo(
    () => ({
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v}%` },
      legend: { type: 'scroll', data: loaded.map((e) => shortName(e.report.name, e.id)) },
      grid: { left: 70, right: 24, top: 36, bottom: 40 },
      xAxis: { type: 'category', data: axis },
      yAxis: { type: 'value', inverse: true, axisLabel: { formatter: '{value}%' } },
      dataZoom: [{ type: 'inside' }],
      series: loaded.map((e, i) => {
        const byDate = new Map(e.report.equity_curve.map((p) => [p.date, p.drawdown]))
        return {
          name: shortName(e.report.name, e.id),
          type: 'line' as const,
          data: axis.map((d) => {
            const v = byDate.get(d)
            return v == null ? null : +(v * 100).toFixed(3)
          }),
          showSymbol: false,
          connectNulls: true,
          lineStyle: { width: 1.5, color: COLORS[i % COLORS.length] },
          itemStyle: { color: COLORS[i % COLORS.length] }
        }
      })
    }),
    [loaded, axis]
  )

  /** 指标并排：每行一个指标，每列一个任务；行内标出最优/最差 */
  const metricRows = useMemo<MetricRow[]>(() => {
    return METRIC_DEFS.filter((d) => loaded.some((e) => d.get(e.report.metrics) != null)).map((d) => {
      const nums = loaded.map((e) => {
        const v = d.get(e.report.metrics)
        return v == null || Number.isNaN(v) ? null : v
      })
      const valid = nums.filter((v): v is number => v != null)
      const distinct = new Set(valid).size
      let bestIdx = -1
      let worstIdx = -1
      if (d.better && distinct > 1) {
        const pick = (f: (a: number, b: number) => boolean) =>
          nums.reduce<number>((acc, v, i) => (v != null && (acc < 0 || f(v, nums[acc] as number)) ? i : acc), -1)
        bestIdx = d.better === 'high' ? pick((a, b) => a > b) : pick((a, b) => a < b)
        worstIdx = d.better === 'high' ? pick((a, b) => a < b) : pick((a, b) => a > b)
      }
      return {
        key: d.key,
        label: d.label,
        cells: nums.map((v, i) => ({
          text: d.render(v),
          flag: i === bestIdx ? 'best' : i === worstIdx ? 'worst' : null
        }))
      }
    })
  }, [loaded])

  const paramLabelMap = useMemo(() => {
    const m: Record<string, string> = {}
    strategies.forEach((s) => s.param_schema?.forEach((p) => {
      if (p.label) m[p.key] = p.label
    }))
    return m
  }, [strategies])
  const riskLabelMap = useMemo(() => {
    const m: Record<string, string> = {}
    RISK_FIELDS.forEach((f) => {
      m[f.key] = f.label
    })
    return m
  }, [])

  /** 参数差异：所有任务配置拍平后按 key 并集对比，行内出现 >1 种取值即"不同" */
  const diffRows = useMemo<DiffRow[]>(() => {
    const maps = loaded.map((e) => flattenBacktestConfig(e.report.config))
    const keys = new Set<string>()
    maps.forEach((m) => m.forEach((_, k) => keys.add(k)))
    const order: Record<string, number> = { 策略参数: 0, 风控: 1, 基础: 2 }
    const rows = [...keys].map((k) => {
      const group = maps.find((m) => m.get(k))?.get(k)?.group ?? ''
      const rawKey = k.split('\u0000')[1]
      let label = rawKey
      if (group === '策略参数') label = paramLabelMap[rawKey] ?? rawKey
      else if (group === '风控') label = riskLabelMap[rawKey] ?? rawKey
      const values = maps.map((m) => fmtDiffVal(m.get(k)?.value))
      return { key: k, group, label, values, diff: new Set(values).size > 1 }
    })
    return rows.sort(
      (x, y) =>
        (order[x.group] ?? 9) - (order[y.group] ?? 9) ||
        Number(y.diff) - Number(x.diff) ||
        x.label.localeCompare(y.label)
    )
  }, [loaded, paramLabelMap, riskLabelMap])

  const shownDiffRows = useMemo(() => (onlyDiff ? diffRows.filter((r) => r.diff) : diffRows), [diffRows, onlyDiff])

  const removeId = (id: string) => {
    const next = ids.filter((x) => x !== id)
    setParams(next.length ? { ids: next.join(',') } : {})
  }

  const metricColumns: ColumnsType<MetricRow> = [
    {
      title: '指标',
      dataIndex: 'label',
      width: 150,
      fixed: 'left',
      render: (v: string, r) => (
        <Tooltip title={r.key}>
          <span>{v}</span>
        </Tooltip>
      )
    },
    ...loaded.map((e, i) => ({
      title: (
        <span style={{ color: COLORS[i % COLORS.length] }}>{shortName(e.report.name, e.id)}</span>
      ),
      key: e.id,
      align: 'right' as const,
      render: (_: unknown, r: MetricRow) => {
        const c = r.cells[i]
        if (!c) return '-'
        const style =
          c.flag === 'best'
            ? { color: '#1677ff', fontWeight: 600 }
            : c.flag === 'worst'
              ? { color: '#bfbfbf' }
              : undefined
        return <span style={style}>{c.text}</span>
      }
    }))
  ]

  const diffColumns: ColumnsType<DiffRow> = [
    { title: '分组', dataIndex: 'group', width: 84, render: (v: string) => <Tag>{v}</Tag> },
    {
      title: '参数',
      dataIndex: 'label',
      width: 150,
      render: (v: string, r) => (
        <Tooltip title={r.key.split('\u0000')[1]}>
          <span>{v}</span>
        </Tooltip>
      )
    },
    ...loaded.map((e, i) => ({
      title: <span style={{ color: COLORS[i % COLORS.length] }}>{shortName(e.report.name, e.id)}</span>,
      key: e.id,
      render: (_: unknown, r: DiffRow) => (
        <span style={r.diff ? { color: '#cf1322', fontWeight: 500 } : undefined}>{r.values[i]}</span>
      )
    }))
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => navigate('/backtests')}>
              返回列表
            </Button>
            <span>多回测对比（{loaded.length}）</span>
          </Space>
        }
        extra={
          <Space size="small" wrap>
            {loaded.map((e, i) => (
              <Tag
                key={e.id}
                color="default"
                closable
                onClose={(ev) => {
                  ev.preventDefault()
                  removeId(e.id)
                }}
              >
                <span style={{ color: COLORS[i % COLORS.length], fontWeight: 600 }}>■</span> {e.report.name}
                <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
                  {e.id}
                </Typography.Text>
              </Tag>
            ))}
            {entries.length > loaded.length && (
              <Tag icon={<CloseOutlined />} color="error">
                {entries.length - loaded.length} 个报告不可用
              </Tag>
            )}
          </Space>
        }
      >
        {!ids.length ? (
          <Empty description="未选择回测任务：请在回测列表勾选 2 个以上「已完成」任务后点「对比选中」" />
        ) : loading ? (
          <Spin />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {failed.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${failed.length} 个任务报告不可用，已跳过`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {failed.map((e) => (
                      <li key={e.id}>
                        <Typography.Text code>{e.id}</Typography.Text>：{e.error}
                      </li>
                    ))}
                  </ul>
                }
              />
            )}
            {loaded.length < 2 ? (
              <Empty description="可用报告不足 2 个，无法对比" />
            ) : (
              <>
                <Card
                  size="small"
                  title="权益曲线叠加"
                  extra={
                    <Radio.Group size="small" value={mode} onChange={(e) => setMode(e.target.value as CurveMode)}>
                      <Radio.Button value="norm">归一化净值（起点=1）</Radio.Button>
                      <Radio.Button value="adjusted">调整净值（含出金）</Radio.Button>
                      <Radio.Button value="raw">原始权益</Radio.Button>
                    </Radio.Group>
                  }
                >
                  <EchartsReact option={curveOption} height={380} />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    不同回测区间取日期并集，缺口处曲线断开（同一交易日的对比仍然有效）；
                    {mode === 'raw' ? '原始权益受初始资金影响，跨资金规模比较请切「归一化净值」。' : '基准线仅在所有任务用同一基准指数时叠加。'}
                  </Typography.Text>
                </Card>

                <Card size="small" title="回撤叠加（同口径：账户权益回撤 %）">
                  <EchartsReact option={drawdownOption} height={220} />
                </Card>

                <Card size="small" title="指标并排" extra={
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    蓝色=行内最优，灰色=行内最差（仅对越高越好/越低越好的指标标注）
                  </Typography.Text>
                }>
                  <Table<MetricRow>
                    rowKey="key"
                    size="small"
                    dataSource={metricRows}
                    columns={metricColumns}
                    pagination={false}
                    scroll={{ x: true }}
                  />
                </Card>

                <Card
                  size="small"
                  title={`参数差异（${diffRows.filter((r) => r.diff).length} 项不同 / 共 ${diffRows.length} 项）`}
                  extra={
                    <Space size="small">
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        红色 = 各任务取值不同
                      </Typography.Text>
                      <Switch checked={onlyDiff} onChange={setOnlyDiff} size="small" />
                      <Typography.Text style={{ fontSize: 12 }}>只看差异项</Typography.Text>
                    </Space>
                  }
                >
                  <Table<DiffRow>
                    rowKey="key"
                    size="small"
                    dataSource={shownDiffRows}
                    columns={diffColumns}
                    pagination={false}
                    scroll={{ x: true }}
                  />
                </Card>
              </>
            )}
          </Space>
        )}
      </Card>
    </Space>
  )
}
