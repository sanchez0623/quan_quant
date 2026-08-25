import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  InputNumber,
  message,
  Modal,
  Progress,
  Radio,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography
} from 'antd'
import { SyncOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { createDemoData, errDetail, getDataStatus, updateData } from '../api/client'
import type { DataSourceHealth } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import { fmtInt } from '../utils/format'

function HealthyTag({ healthy }: { healthy: boolean | null }) {
  if (healthy === true) return <Tag color="success">正常</Tag>
  if (healthy === false) return <Tag color="error">异常</Tag>
  return <Tag color="default">未检测</Tag>
}

export default function DataManagement() {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof getDataStatus>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [scope, setScope] = useState<'daily' | 'minute5' | 'all'>('daily')
  const [task, setTask] = useState<{ id: string; label: string } | null>(null)
  const [demoDays, setDemoDays] = useState<number>(500)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await getDataStatus())
    } catch (err) {
      message.error(errDetail(err, '加载数据状态失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const { progress, message: taskMessage } = useTaskProgress(task?.id ?? null, (s) => {
    const label = task?.label ?? '任务'
    if (s === 'success') {
      message.success(`${label}完成`)
    } else {
      message.error(`${label}失败`)
    }
    setTask(null)
    refresh()
  })

  const onUpdate = async () => {
    try {
      const res = await updateData(scope)
      setTask({ id: res.task_id, label: '数据更新' })
      message.info('更新任务已提交')
    } catch (err) {
      message.error(errDetail(err, '提交更新失败'))
    }
  }

  const onDemo = () => {
    Modal.confirm({
      title: '确认生成演示数据？',
      content: (
        <div>
          <p>将生成合成数据（随机游走+趋势）供无真实数据源环境的联调与演示。</p>
          <p>幂等：重复调用会覆盖生成。</p>
        </div>
      ),
      okText: '生成',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await createDemoData({ days: demoDays })
          setTask({ id: res.task_id, label: '演示数据生成' })
          message.info('生成任务已提交')
        } catch (err) {
          message.error(errDetail(err, '提交生成失败'))
        }
      }
    })
  }

  const sourceColumns: ColumnsType<DataSourceHealth> = [
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '角色', dataIndex: 'role' },
    {
      title: '健康状态',
      dataIndex: 'healthy',
      width: 110,
      render: (v: boolean | null) => <HealthyTag healthy={v} />
    },
    {
      title: '最后检查时间',
      dataIndex: 'last_check',
      width: 180,
      render: (v: string | null) => v ?? '-'
    },
    { title: '备注', dataIndex: 'note', render: (v: string) => v || '-' }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={6}>
          <Card size="small" title="日线（daily）" loading={loading}>
            <Statistic title="股票数" value={fmtInt(status?.daily.stocks)} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="数据行数"
              value={fmtInt(status?.daily.rows)}
              valueStyle={{ fontSize: 20 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              起止：{status?.daily.start ?? '-'} ~ {status?.daily.end ?? '-'}
              <br />
              更新：{status?.daily.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="5分钟线（minute5）" loading={loading}>
            <Statistic title="股票数" value={fmtInt(status?.minute5.stocks)} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="数据行数"
              value={fmtInt(status?.minute5.rows)}
              valueStyle={{ fontSize: 20 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              起止：{status?.minute5.start ?? '-'} ~ {status?.minute5.end ?? '-'}
              <br />
              更新：{status?.minute5.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="复权因子（adj_factor）" loading={loading}>
            <Statistic title="数据行数" value={fmtInt(status?.adj_factor.rows)} valueStyle={{ fontSize: 20 }} />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              更新：{status?.adj_factor.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="交易日历（calendar）" loading={loading}>
            <Statistic
              title="起止日期"
              value={`${status?.calendar.start ?? '-'} ~ ${status?.calendar.end ?? '-'}`}
              valueStyle={{ fontSize: 16 }}
            />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="数据源健康状态">
        <Table<DataSourceHealth>
          rowKey="name"
          size="small"
          pagination={false}
          loading={loading}
          dataSource={status?.sources ?? []}
          columns={sourceColumns}
        />
      </Card>

      <Card size="small" title="数据操作">
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div>
            <Typography.Text strong>增量更新</Typography.Text>
            <div style={{ marginTop: 8 }}>
              <Space>
                <Radio.Group
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                  optionType="button"
                  options={[
                    { value: 'daily', label: '日线' },
                    { value: 'minute5', label: '5分钟线' },
                    { value: 'all', label: '全部' }
                  ]}
                />
                <Button
                  type="primary"
                  icon={<SyncOutlined />}
                  disabled={!!task}
                  loading={!!task}
                  onClick={onUpdate}
                >
                  开始更新
                </Button>
              </Space>
            </div>
          </div>
          <div>
            <Typography.Text strong>生成演示数据</Typography.Text>
            <div style={{ marginTop: 8 }}>
              <Space>
                <Typography.Text type="secondary">天数：</Typography.Text>
                <InputNumber min={30} max={5000} value={demoDays} onChange={(v) => setDemoDays(v ?? 500)} />
                <Button danger icon={<ThunderboltOutlined />} disabled={!!task} onClick={onDemo}>
                  生成演示数据
                </Button>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  生成合成数据（随机游走+趋势），供无真实数据源环境联调演示，重复调用覆盖生成。
                </Typography.Text>
              </Space>
            </div>
          </div>
          {task && (
            <div>
              <Typography.Text strong>{task.label}进行中</Typography.Text>
              <Progress percent={progress} status="active" style={{ maxWidth: 480 }} />
              <Typography.Text type="secondary">{taskMessage || '执行中...'}</Typography.Text>
            </div>
          )}
        </Space>
      </Card>
    </Space>
  )
}
