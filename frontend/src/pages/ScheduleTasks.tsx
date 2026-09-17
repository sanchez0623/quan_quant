import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Badge, Button, Card, Progress, Radio, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { getScheduleStatus, errDetail } from '../api/client'
import type { ScheduleStatus, ScheduleTaskItem, TaskStatus } from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'

const TYPE_LABEL: Record<string, string> = {
  data_update: '数据更新',
  live_premarket: '盘前流程',
  live_postclose: '盘后流程'
}

const SCOPE_LABEL: Record<string, string> = {
  daily: '日线',
  minute5: '分钟线',
  all: '全量',
  industry: '行业成分',
  stock_basic: '股票元数据',
  calendar: '交易日历',
  index_daily: '基准指数'
}

const SLOT_LABEL: Record<string, string> = {
  morning: '盘前流程（08:25）',
  postclose: '盘后流程（15:25）',
  evening: '盘后日线更新（18:10）',
  minute5: '盘后分钟线更新（日线完成后）'
}

const TYPE_FILTERS = [
  { label: '全部', value: 'all' },
  { label: '数据更新', value: 'data_update' },
  { label: '盘前流程', value: 'live_premarket' },
  { label: '盘后流程', value: 'live_postclose' }
]

function durationOf(row: ScheduleTaskItem): string {
  if (!row.created_at || !row.finished_at) return '—'
  const start = new Date(row.created_at.replace(' ', 'T')).getTime()
  const end = new Date(row.finished_at.replace(' ', 'T')).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—'
  const sec = Math.round((end - start) / 1000)
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m${sec % 60}s`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}

function typeLabel(t: ScheduleTaskItem): string {
  const base = TYPE_LABEL[t.type] ?? t.type
  const scope = (t.payload as { scope?: string } | null)?.scope
  return scope && SCOPE_LABEL[scope] ? `${base}·${SCOPE_LABEL[scope]}` : base
}

/**
 * 从 message 解析「阶段 + 阶段内进度」。
 * 任务级 progress 是硬编码区段（日线 5~75%、分钟线 78~95%），与真实耗时比例
 * 严重失真；而 message 自带阶段内计数（如 "正在拉取分钟线: 000656 (70/2923)"），
 * 用它算出的百分比才是阶段内真实进度。
 */
export function parseStage(msg?: string | null): { label: string; done?: number; total?: number } | null {
  if (!msg) return null
  const m = msg.match(/(日线|分钟线)[^()]*\((\d+)\/(\d+)\)/)
  if (m) return { label: m[1], done: Number(m[2]), total: Number(m[3]) }
  const f = msg.match(/分批落库\s*(\d+)\/(\d+)/)
  if (f) return { label: '分批落库', done: Number(f[1]), total: Number(f[2]) }
  if (msg.includes('健康检查')) return { label: '健康检查' }
  return null
}

export default function ScheduleTasks() {
  const [data, setData] = useState<ScheduleStatus | null>(null)
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await getScheduleStatus())
      setErr('')
    } catch (e) {
      setErr(errDetail(e, '加载定时任务状态失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 15_000)
    return () => clearInterval(timer)
  }, [load])

  const rows = useMemo(() => {
    const all = data?.tasks ?? []
    return typeFilter === 'all' ? all : all.filter((t) => t.type === typeFilter)
  }, [data, typeFilter])

  const columns: ColumnsType<ScheduleTaskItem> = [
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 165,
      render: (v: string) => (v ? v.slice(5) : '—')
    },
    { title: '任务', dataIndex: 'name', ellipsis: true },
    {
      title: '类型',
      key: 'type',
      width: 130,
      render: (_, r) => <Tag>{typeLabel(r)}</Tag>
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 95,
      render: (s: TaskStatus) => <TaskStatusTag status={s} />
    },
    {
      title: '进度（阶段内）',
      dataIndex: 'progress',
      width: 185,
      render: (_: number, r) => {
        if (r.status !== 'running') {
          return <span>{Math.round(r.progress)}%</span>
        }
        const st = parseStage(r.message)
        // 阶段内计数可得时用真实百分比；否则回退任务级 progress（趋势参考）
        const percent =
          st?.done != null && st.total
            ? Math.round((st.done / st.total) * 100)
            : Math.round(r.progress)
        return (
          <div>
            {st && (
              <Tag color="blue" style={{ marginInlineEnd: 0, marginBottom: 2 }}>
                {st.label}
                {st.done != null && st.total ? ` ${st.done}/${st.total}` : ''}
              </Tag>
            )}
            <Tooltip title={r.message ?? ''}>
              <Progress percent={percent} size="small" status="active" />
            </Tooltip>
          </div>
        )
      }
    },
    {
      title: '当前消息',
      dataIndex: 'message',
      ellipsis: true,
      render: (v: string | null) => v ?? '—'
    },
    { title: '耗时', key: 'dur', width: 90, render: (_, r) => durationOf(r) },
    { title: '完成时间', dataIndex: 'finished_at', width: 165, render: (v: string | null) => (v ? v.slice(5) : '—') }
  ]

  const submitted = data?.submitted_today

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {err && <Alert type="error" showIcon message={err} />}

      <Card
        title="调度状态"
        extra={
          <Button icon={<ReloadOutlined />} size="small" onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space size={24} wrap>
          <span>
            调度器：
            {data ? (
              <Badge
                status={data.auto_schedule ? 'processing' : 'default'}
                text={data.auto_schedule ? '运行中' : '已关闭（auto_schedule=off）'}
              />
            ) : (
              '…'
            )}
          </span>
          <span>今日（{data?.today ?? '—'}）提交状态：</span>
          {submitted &&
            Object.entries(SLOT_LABEL).map(([k, label]) => (
              <Tag key={k} color={submitted[k as keyof typeof submitted] ? 'success' : 'default'}>
                {label}{submitted[k as keyof typeof submitted] ? '·已提交' : '·未提交'}
              </Tag>
            ))}
        </Space>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0, marginTop: 12 }}>
          盘后 18:10 自动拉取日线，日线任务结束后自动跟随提交分钟线增量（错峰串行，防数据源并发限流）；
          分钟线任务与日线互相独立，日线失败不影响分钟线照跑。
        </Typography.Paragraph>
      </Card>

      <Card
        title="定时任务记录（最近 50 条）"
        extra={
          <Radio.Group
            size="small"
            optionType="button"
            options={TYPE_FILTERS}
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
          />
        }
      >
        <Table<ScheduleTaskItem>
          rowKey="task_id"
          size="small"
          loading={loading && !data}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 15, showSizeChanger: false, showTotal: (n) => `共 ${n} 条` }}
          expandable={{
            rowExpandable: (r) => Boolean(r.error),
            expandedRowRender: (r) => (
              <Typography.Paragraph type="danger" style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                {r.error}
              </Typography.Paragraph>
            )
          }}
          rowClassName={(r) => (r.status === 'failed' ? 'schedule-row-failed' : '')}
        />
      </Card>
    </Space>
  )
}
