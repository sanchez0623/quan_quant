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
  Popconfirm,
  Progress,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography
} from 'antd'
import { ArrowLeftOutlined, RedoOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import {
  createBacktest,
  createTemplate,
  errDetail,
  getOptimizeDetail,
  resumeOptimize
} from '../api/client'
import type { BacktestCreateRequest, OptimizeDetail, TrialItem } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import TaskStatusTag from '../components/TaskStatusTag'
import ImportanceBar from '../components/ImportanceBar'
import ParallelChart from '../components/ParallelChart'
import { RISK_FIELDS } from '../components/RiskConfigForm'
import { fmtNum, fmtPct } from '../utils/format'

/** 风控字段集合：寻优若搜了风控参数，best_params 里会混着它们，需归位到 risk_config */
const RISK_KEYS = new Set(RISK_FIELDS.map((f) => f.key))

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

const ROBUST_COLOR: Record<string, string> = { robust: 'green', fragile: 'red', unknown: 'default' }
const ROBUST_TEXT: Record<string, string> = {
  robust: '稳健（跨池/换时段均达标）',
  fragile: '不稳健（依赖特定股票池/时段）',
  unknown: '无法判定（数据不足）'
}

export default function OptimizeDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<OptimizeDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [rerunning, setRerunning] = useState(false)
  const [resuming, setResuming] = useState(false)

  const doResume = async () => {
    if (!id) return
    setResuming(true)
    try {
      await resumeOptimize(id)
      message.success('已重新提交续跑，Optuna 将载入已有 trial 继续')
      load()
    } catch (err) {
      message.error(errDetail(err, '续跑失败'))
    } finally {
      setResuming(false)
    }
  }

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

  /** 组装"最优参数"完整回测配置：best_params 里混着的风控字段拆回 risk_config */
  const buildBestConfig = useCallback((): BacktestCreateRequest | null => {
    if (!detail?.backtest_config) return null
    const cfg = detail.backtest_config
    const best = detail.best_params ?? {}
    const bestParams: Record<string, string | number | boolean> = {}
    const bestRisk: Record<string, string | number> = {}
    Object.entries(best).forEach(([k, v]) => {
      if (RISK_KEYS.has(k)) (bestRisk as Record<string, string | number>)[k] = v as string | number
      else bestParams[k] = v
    })
    return {
      ...cfg,
      params: { ...(cfg.params ?? {}), ...bestParams },
      risk_config: { ...(cfg.risk_config ?? {}), ...bestRisk }
    }
  }, [detail])

  const rerunBest = async () => {
    const cfg = buildBestConfig()
    if (!cfg) {
      message.warning('该任务未返回原始回测配置（backtest_config），无法直接重跑')
      return
    }
    setRerunning(true)
    try {
      const res = await createBacktest({
        ...cfg,
        name: `${detail?.name || '寻优'}-最优参数重跑`
      })
      message.success('已用最优参数创建回测任务')
      navigate(`/backtests/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建回测失败'))
    } finally {
      setRerunning(false)
    }
  }

  /** 寻优结果一键存为回测模板：闭环「寻优 → 模板 → 验证/复用」 */
  const saveBestAsTemplate = async () => {
    const cfg = buildBestConfig()
    if (!cfg) {
      message.warning('该任务未返回原始回测配置（backtest_config），无法存为模板')
      return
    }
    const name = `${detail?.name || '寻优'}-最优参数`
    try {
      await createTemplate({ name, config: { ...cfg, name } })
      message.success(`已存为模板「${name}」，可在新建回测页载入`)
    } catch (err) {
      message.error(errDetail(err, '存为模板失败'))
    }
  }

  const trialColumns: ColumnsType<TrialItem> = [
    { title: '#', dataIndex: 'number', width: 60 },
    {
      title: '组 / 轮',
      width: 110,
      render: (_, r) => (r.group ? `${r.group} · R${r.round}` : '-')
    },
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
          {detail.status !== 'success' && (
            <Popconfirm
              title="确认断点续传？"
              description="用同一任务ID重新提交，Optuna 载入已有 trial，只补跑剩余部分。"
              onConfirm={doResume}
              disabled={resuming}
            >
              <Button icon={<RedoOutlined />} loading={resuming} type="primary" danger>
                断点续传
              </Button>
            </Popconfirm>
          )}
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
                <Space style={{ marginTop: 12 }}>
                  <Button
                    type="primary"
                    icon={<ReloadOutlined />}
                    loading={rerunning}
                    onClick={rerunBest}
                  >
                    用最优参数重跑回测
                  </Button>
                  <Button icon={<SaveOutlined />} onClick={saveBestAsTemplate}>
                    存为模板
                  </Button>
                </Space>
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

          {detail.robustness && (
            <Card
              size="small"
              title="稳健性验证（跨池 / 换时段）"
              extra={
                <Tag color={ROBUST_COLOR[detail.robustness.verdict] ?? 'default'}>
                  {ROBUST_TEXT[detail.robustness.verdict] ?? detail.robustness.verdict}
                </Tag>
              }
            >
              {detail.robustness.reason && (
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
                  {detail.robustness.reason}
                </Typography.Paragraph>
              )}
              {(detail.robustness.cross_pool ?? []).length > 0 && (
                <Table
                  size="small"
                  pagination={false}
                  style={{ marginBottom: 16 }}
                  rowKey={(r) => r.name}
                  dataSource={(detail.robustness.cross_pool ?? []).map((r, i) => ({ key: i, ...r }))}
                  columns={[
                    { title: '换池样本', dataIndex: 'name' },
                    {
                      title: '池规模',
                      width: 80,
                      render: (_, r) => r.universe?.length ?? '-'
                    },
                    {
                      title: '年化',
                      dataIndex: 'annual_return',
                      width: 110,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '总收益',
                      dataIndex: 'total_return',
                      width: 100,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '回撤',
                      dataIndex: 'max_drawdown',
                      width: 100,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '夏普',
                      dataIndex: 'sharpe',
                      width: 90,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtNum(v, 2))
                    },
                    {
                      title: '说明',
                      dataIndex: 'skipped',
                      width: 120,
                      render: (v: string | null | undefined) => (v ? <Tag>{v}</Tag> : '')
                    }
                  ]}
                  title={() => '换池（同窗口 · 另选创业板/科创板）'}
                />
              )}
              {(detail.robustness.cross_period ?? []).length > 0 && (
                <Table
                  size="small"
                  pagination={false}
                  dataSource={(detail.robustness.cross_period ?? []).map((r, i) => ({ key: i, ...r }))}
                  columns={[
                    { title: '换时段样本', dataIndex: 'label' },
                    {
                      title: '年化',
                      dataIndex: 'annual_return',
                      width: 110,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '总收益',
                      dataIndex: 'total_return',
                      width: 100,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '回撤',
                      dataIndex: 'max_drawdown',
                      width: 100,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtPct(v))
                    },
                    {
                      title: '夏普',
                      dataIndex: 'sharpe',
                      width: 90,
                      align: 'right',
                      render: (v: number | null | undefined) => (v == null ? '-' : fmtNum(v, 2))
                    },
                    {
                      title: '说明',
                      dataIndex: 'skipped',
                      width: 120,
                      render: (v: string | null | undefined) => (v ? <Tag>{v}</Tag> : '')
                    }
                  ]}
                  title={() => '换时段（同池 · 最近两个完整年度）'}
                />
              )}
            </Card>
          )}

          {detail.groups_schedule && detail.groups_schedule.length > 0 && (
            <Card
              size="small"
              title="分组坐标轮换（方案A）"
              extra={
                detail.objective ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    目标 {METRIC_LABEL[detail.objective.metric] ?? detail.objective.metric} ·
                    切窗 {detail.objective.n_windows} · λ {detail.objective.variance_penalty}
                    {detail.objective.dd_floor != null
                      ? ` · 回撤熔断 ${detail.objective.dd_floor}`
                      : ''}
                  </Typography.Text>
                ) : null
              }
            >
              <Table
                size="small"
                pagination={false}
                style={{ marginBottom: 16 }}
                dataSource={(detail.groups_schedule ?? []).map((g, i) => ({
                  key: i,
                  ...g,
                  param_count: Object.keys(g.params ?? {}).length
                }))}
                columns={[
                  { title: '组', dataIndex: 'name' },
                  { title: '每轮试验数', dataIndex: 'n_trials', width: 110, align: 'right' },
                  { title: '搜索参数数', dataIndex: 'param_count', width: 110, align: 'right' }
                ]}
                title={() => '分组计划'}
              />
              {detail.rounds_history && detail.rounds_history.length > 0 && (
                <Table
                  size="small"
                  pagination={false}
                  style={{ marginBottom: 16 }}
                  dataSource={detail.rounds_history}
                  columns={[
                    { title: '轮次', dataIndex: 'round', width: 60 },
                    {
                      title: '最优目标值',
                      dataIndex: 'best_value',
                      width: 120,
                      align: 'right',
                      render: (v: number) => fmtNum(v, 4)
                    },
                    {
                      title: '是否有提升',
                      dataIndex: 'improved',
                      width: 100,
                      render: (v: boolean) => (v ? <Tag color="green">是</Tag> : <Tag>否</Tag>)
                    },
                    {
                      title: '各组最优',
                      render: (_, r) => (
                        <Typography.Text style={{ fontSize: 12 }}>
                          {Object.entries(r.groups ?? {})
                            .map(([k, v]) => `${k}=${fmtNum(v, 4)}`)
                            .join('  ')}
                        </Typography.Text>
                      )
                    }
                  ]}
                  title={() => '轮次历史'}
                />
              )}
              {detail.per_group_best && detail.per_group_best.length > 0 && (
                <Table
                  size="small"
                  pagination={false}
                  dataSource={detail.per_group_best}
                  columns={[
                    { title: '组', dataIndex: 'group' },
                    { title: '轮次', dataIndex: 'round', width: 60 },
                    { title: '试验数', dataIndex: 'n_trials', width: 80, align: 'right' },
                    {
                      title: '组内最优',
                      dataIndex: 'best_value',
                      width: 110,
                      align: 'right',
                      render: (v: number) => fmtNum(v, 4)
                    },
                    {
                      title: '参数',
                      dataIndex: 'params',
                      ellipsis: true,
                      render: (v: Record<string, string | number | boolean>) => (
                        <Typography.Text code style={{ fontSize: 12 }}>
                          {JSON.stringify(v)}
                        </Typography.Text>
                      )
                    }
                  ]}
                  title={() => '各组最优参数'}
                />
              )}
            </Card>
          )}

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
