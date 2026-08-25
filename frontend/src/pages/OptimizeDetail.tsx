import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  message,
  Progress,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography
} from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { createBacktest, errDetail, getOptimizeDetail } from '../api/client'
import type { OptimizeDetail, TrialItem } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import TaskStatusTag from '../components/TaskStatusTag'
import ImportanceBar from '../components/ImportanceBar'
import ParallelChart from '../components/ParallelChart'
import { fmtNum, fmtPct } from '../utils/format'

const METRIC_LABEL: Record<string, string> = {
  annual_return: '年化收益',
  sharpe: '夏普比率',
  calmar: '卡玛比率',
  total_return: '总收益'
}

function fmtMetricValue(metric: string, v?: number | null): string {
  if (v === null || v === undefined) return '-'
  if (metric === 'sharpe' || metric === 'calmar') return fmtNum(v, 4)
  return fmtPct(v)
}

const TRIAL_STATE: Record<string, { color: string; text: string }> = {
  complete: { color: 'success', text: '完成' },
  running: { color: 'processing', text: '进行中' },
  waiting: { color: 'default', text: '等待' },
  pruned: { color: 'default', text: '剪枝' },
  fail: { color: 'error', text: '失败' }
}

const RISK_COLOR: Record<string, string> = { high: 'red', medium: 'gold', low: 'green' }
const RISK_TEXT: Record<string, string> = { high: '高', medium: '中', low: '低' }

export default function OptimizeDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<OptimizeDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [rerunning, setRerunning] = useState(false)

  const load = useCallback(async () => {
    if (!id) return
    try {
      setDetail(await getOptimizeDetail(id))
    } catch (err) {
      message.error(errDetail(err, '加载寻优任务失败'))
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  const active = detail?.status === 'pending' || detail?.status === 'running'

  // 运行中每 3s 刷新已完成 trials
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(load, 3000)
    return () => window.clearInterval(timer)
  }, [active, load])

  const { progress, message: wsMessage } = useTaskProgress(active && id ? id : null, (s) => {
    if (s === 'success') message.success('寻优完成')
    else if (s === 'failed') message.error('寻优失败')
    load()
  })

  const rerunBest = async () => {
    if (!detail?.backtest_config) {
      message.warning('该任务未返回原始回测配置（backtest_config），无法直接重跑')
      return
    }
    const cfg = detail.backtest_config
    setRerunning(true)
    try {
      const res = await createBacktest({
        ...cfg,
        name: `${detail.name || '寻优'}-最优参数重跑`,
        params: { ...(cfg.params ?? {}), ...(detail.best_params ?? {}) }
      })
      message.success('已用最优参数创建回测任务')
      navigate(`/backtests/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建回测失败'))
    } finally {
      setRerunning(false)
    }
  }

  const trialColumns: ColumnsType<TrialItem> = [
    { title: '#', dataIndex: 'number', width: 60 },
    {
      title: '参数',
      dataIndex: 'params',
      ellipsis: true,
      render: (v: Record<string, string | number | boolean>) => (
        <Typography.Text code style={{ fontSize: 12 }}>
          {JSON.stringify(v)}
        </Typography.Text>
      )
    },
    {
      title: '目标值',
      dataIndex: 'value',
      width: 100,
      align: 'right',
      render: (v: number | null) => fmtMetricValue(detail?.metric ?? '', v)
    },
    {
      title: '状态',
      dataIndex: 'state',
      width: 80,
      render: (s: string) => (
        <Tag color={TRIAL_STATE[s]?.color ?? 'default'}>{TRIAL_STATE[s]?.text ?? s}</Tag>
      )
    },
    {
      title: '样本内',
      dataIndex: 'in_sample_value',
      width: 100,
      align: 'right',
      render: (v: number | null | undefined) => fmtMetricValue(detail?.metric ?? '', v)
    },
    {
      title: '样本外',
      dataIndex: 'out_sample_value',
      width: 100,
      align: 'right',
      render: (v: number | null | undefined) => fmtMetricValue(detail?.metric ?? '', v)
    }
  ]

  if (loading && !detail) {
    return (
      <Card>
        <Spin />
      </Card>
    )
  }

  if (!detail) {
    return (
      <Card>
        <Empty description="寻优任务不存在" />
      </Card>
    )
  }

  const oos = detail.oos_validation
  const oosRows = oos
    ? [
        {
          key: 'annual_return',
          name: '年化收益',
          inS: fmtPct(oos.in_sample.annual_return),
          oos: fmtPct(oos.out_sample.annual_return)
        },
        {
          key: 'max_drawdown',
          name: '最大回撤',
          inS: fmtPct(oos.in_sample.max_drawdown),
          oos: fmtPct(oos.out_sample.max_drawdown)
        },
        {
          key: 'sharpe',
          name: '夏普比率',
          inS: fmtNum(oos.in_sample.sharpe),
          oos: fmtNum(oos.out_sample.sharpe)
        }
      ]
    : []

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card>
        <Space size="middle" wrap>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/optimize')}>
            返回
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {detail.task_id}
          </Typography.Title>
          <TaskStatusTag status={detail.status} />
          <Typography.Text type="secondary">
            指标：{METRIC_LABEL[detail.metric] ?? detail.metric} · 试验数 {detail.n_trials}
          </Typography.Text>
        </Space>
        {active && (
          <div style={{ marginTop: 16 }}>
            <Progress percent={Math.max(progress, detail.progress ?? 0)} status="active" />
            <Typography.Text type="secondary">
              {wsMessage || `已完成 ${detail.trials?.length ?? 0} / ${detail.n_trials} 个试验...`}
            </Typography.Text>
          </div>
        )}
        {detail.status === 'failed' && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message="寻优失败"
            description={detail.error || '无详细错误信息'}
          />
        )}
      </Card>

      {detail.status === 'success' && (
        <>
          <Row gutter={16}>
            <Col span={12}>
              <Card size="small" title="最优参数">
                <Descriptions column={1} size="small">
                  {(Object.entries(detail.best_params ?? {}) as Array<[string, string | number | boolean]>).map(
                    ([k, v]) => (
                      <Descriptions.Item key={k} label={k}>
                        {String(v)}
                      </Descriptions.Item>
                    )
                  )}
                </Descriptions>
                <Descriptions column={1} size="small" style={{ marginTop: 8 }}>
                  <Descriptions.Item label={`最优${METRIC_LABEL[detail.metric] ?? '目标值'}`}>
                    <Typography.Text strong style={{ color: '#cf1322', fontSize: 16 }}>
                      {fmtMetricValue(detail.metric, detail.best_value)}
                    </Typography.Text>
                  </Descriptions.Item>
                </Descriptions>
                <Button
                  type="primary"
                  icon={<ReloadOutlined />}
                  style={{ marginTop: 12 }}
                  loading={rerunning}
                  onClick={rerunBest}
                >
                  用最优参数重跑回测
                </Button>
              </Card>
            </Col>
            <Col span={12}>
              <Card size="small" title="样本外验证（OOS）">
                {oos ? (
                  <>
                    <Table
                      size="small"
                      pagination={false}
                      dataSource={oosRows}
                      columns={[
                        { title: '指标', dataIndex: 'name' },
                        { title: '样本内', dataIndex: 'inS', align: 'right' },
                        { title: '样本外', dataIndex: 'oos', align: 'right' }
                      ]}
                    />
                    <div style={{ marginTop: 12 }}>
                      过拟合风险：
                      <Tag color={RISK_COLOR[oos.overfit_risk] ?? 'default'}>
                        {RISK_TEXT[oos.overfit_risk] ?? oos.overfit_risk}
                      </Tag>
                    </div>
                  </>
                ) : (
                  <Empty description="暂无样本外验证数据" />
                )}
              </Card>
            </Col>
          </Row>

          <Card size="small" title="参数重要性">
            <ImportanceBar data={detail.param_importance ?? {}} />
          </Card>

          <Card size="small" title="试验分布（平行坐标）">
            <ParallelChart trials={detail.trials ?? []} metric={METRIC_LABEL[detail.metric] ?? detail.metric} />
          </Card>
        </>
      )}

      <Card size="small" title={`试验列表（${detail.trials?.length ?? 0}）`}>
        <Table<TrialItem>
          rowKey="number"
          dataSource={detail.trials ?? []}
          columns={trialColumns}
          size="small"
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个试验` }}
        />
      </Card>
    </Space>
  )
}
