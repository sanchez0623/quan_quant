import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  message,
  Radio,
  Row,
  Select,
  Space,
  Table,
  Typography
} from 'antd'
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import {
  createOptimize,
  errDetail,
  getBacktestReport,
  getBacktests,
  getOptimizeList,
  getStrategies
} from '../api/client'
import type {
  BacktestCreateRequest,
  BacktestListItem,
  OptimizeListItem,
  ParamSpaceItem,
  Strategy
} from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import { RISK_FIELDS } from '../components/RiskConfigForm'
import { fmtNum } from '../utils/format'

interface SpaceRow {
  key?: string
  type: 'int' | 'float' | 'select'
  low?: number
  high?: number
  choices?: string
}

interface OptimizeFormValues {
  name: string
  template?: string
  n_trials: number
  metric: 'annual_return' | 'sharpe' | 'calmar' | 'total_return'
  param_space?: SpaceRow[]
}

export default function OptimizeList() {
  const [form] = Form.useForm<OptimizeFormValues>()
  const navigate = useNavigate()
  const [backtests, setBacktests] = useState<BacktestListItem[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [templateConfig, setTemplateConfig] = useState<BacktestCreateRequest | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [list, setList] = useState<OptimizeListItem[]>([])
  const [loadingList, setLoadingList] = useState(true)

  const fetchList = useCallback(async () => {
    try {
      setList(await getOptimizeList())
    } catch {
      /* ignore */
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => {
    fetchList()
  }, [fetchList])

  useEffect(() => {
    getBacktests()
      .then(setBacktests)
      .catch(() => {})
    getStrategies()
      .then(setStrategies)
      .catch(() => {})
  }, [])

  const hasActive = list.some((t) => t.status === 'pending' || t.status === 'running')
  useEffect(() => {
    if (!hasActive) return
    const timer = window.setInterval(() => {
      getOptimizeList()
        .then(setList)
        .catch(() => {})
    }, 3000)
    return () => window.clearInterval(timer)
  }, [hasActive])

  const successBacktests = useMemo(
    () => backtests.filter((b) => b.status === 'success'),
    [backtests]
  )

  const onTemplateChange = async (taskId: string) => {
    setTemplateConfig(null)
    if (!taskId) return
    try {
      const report = await getBacktestReport(taskId)
      setTemplateConfig(report.config)
      const curName = form.getFieldValue('name')
      if (!curName) {
        form.setFieldsValue({ name: `${report.name}-寻优` })
      }
    } catch (err) {
      message.error(errDetail(err, '读取回测配置失败'))
    }
  }

  const paramOptions = useMemo(() => {
    if (!templateConfig) return []
    const s = strategies.find((x) => x.id === templateConfig.strategy_id)
    const opts = (s?.param_schema ?? []).map((p) => ({
      value: p.key,
      label: `策略参数 · ${p.label}（${p.key}）`
    }))
    return [
      ...opts,
      ...RISK_FIELDS.map((f) => ({ value: f.key, label: `风控 · ${f.label}（${f.key}）` }))
    ]
  }, [templateConfig, strategies])

  const onFinish = async (values: OptimizeFormValues) => {
    if (!templateConfig) {
      message.warning('请先选择模板回测')
      return
    }
    const space: Record<string, ParamSpaceItem> = {}
    for (const row of values.param_space ?? []) {
      if (!row?.key) continue
      if (row.type === 'select') {
        const choices = String(row.choices ?? '')
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean)
        if (choices.length === 0) {
          message.error(`参数 ${row.key} 的候选值不能为空`)
          return
        }
        space[row.key] = { type: 'select', choices }
      } else {
        if (row.low === undefined || row.low === null || row.high === undefined || row.high === null) {
          message.error(`参数 ${row.key} 需要填写 low / high 范围`)
          return
        }
        if (row.low >= row.high) {
          message.error(`参数 ${row.key} 的 low 必须小于 high`)
          return
        }
        space[row.key] = { type: row.type, low: row.low, high: row.high }
      }
    }
    if (Object.keys(space).length === 0) {
      message.warning('请至少配置一个搜索参数')
      return
    }
    setSubmitting(true)
    try {
      const res = await createOptimize({
        name: values.name,
        backtest_config: { ...templateConfig },
        param_space: space,
        n_trials: values.n_trials ?? 50,
        metric: values.metric ?? 'annual_return'
      })
      message.success('寻优任务已创建')
      navigate(`/optimize/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建寻优任务失败'))
    } finally {
      setSubmitting(false)
    }
  }

  const columns: ColumnsType<OptimizeListItem> = [
    { title: '任务名', dataIndex: 'name', ellipsis: true },
    { title: '任务ID', dataIndex: 'task_id', width: 130, ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (v: OptimizeListItem['status']) => <TaskStatusTag status={v} />
    },
    {
      title: '试验数',
      dataIndex: 'n_trials',
      width: 80,
      align: 'right'
    },
    {
      title: '最优值',
      dataIndex: 'best_value',
      width: 110,
      align: 'right',
      render: (v: number | null | undefined) =>
        v === null || v === undefined ? '-' : fmtNum(v, 4)
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-')
    },
    {
      title: '操作',
      width: 90,
      render: (_, record) => (
        <Button type="link" size="small" onClick={() => navigate(`/optimize/${record.task_id}`)}>
          查看
        </Button>
      )
    }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card title="新建参数寻优">
        <Form<OptimizeFormValues>
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{ n_trials: 50, metric: 'annual_return' }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请输入任务名称' }]}>
                <Input placeholder="例如：双均线参数寻优" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item
                name="template"
                label="模板回测（取其配置）"
                rules={[{ required: true, message: '请选择模板回测' }]}
              >
                <Select
                  placeholder="选择一个已成功的回测作为配置模板"
                  showSearch
                  optionFilterProp="label"
                  options={successBacktests.map((b) => ({
                    value: b.task_id,
                    label: `${b.name}（${b.task_id}）`
                  }))}
                  onChange={onTemplateChange}
                  allowClear
                />
              </Form.Item>
            </Col>
            <Col span={3}>
              <Form.Item name="n_trials" label="试验数 n_trials" rules={[{ required: true }]}>
                <InputNumber min={1} max={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={3}>
              <Form.Item name="metric" label="优化指标">
                <Select
                  options={[
                    { value: 'annual_return', label: '年化收益' },
                    { value: 'sharpe', label: '夏普比率' },
                    { value: 'calmar', label: '卡玛比率' },
                    { value: 'total_return', label: '总收益' }
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>

          {templateConfig && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={`模板配置：策略 ${templateConfig.strategy_id} · 周期 ${
                templateConfig.period
              } · 股票池 ${templateConfig.universe?.length ?? 0} 只 · ${templateConfig.start_date} ~ ${
                templateConfig.end_date
              }`}
            />
          )}

          <Form.Item
            label="参数搜索空间"
            required
            style={{ marginBottom: 8 }}
          >
            <Form.List name="param_space" initialValue={[{ type: 'int' }]}>
              {(fields, { add, remove }) => (
                <div>
                  {fields.map((f) => (
                    <Row key={f.key} gutter={8} style={{ marginBottom: 8 }} align="middle">
                      <Col span={9}>
                        <Form.Item
                          name={[f.name, 'key']}
                          rules={[{ required: true, message: '请选择参数' }]}
                          style={{ marginBottom: 0 }}
                        >
                          <Select
                            placeholder="参数名"
                            options={paramOptions}
                            showSearch
                            optionFilterProp="label"
                            disabled={!templateConfig}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={4}>
                        <Form.Item
                          name={[f.name, 'type']}
                          rules={[{ required: true }]}
                          style={{ marginBottom: 0 }}
                        >
                          <Select
                            options={[
                              { value: 'int', label: 'int' },
                              { value: 'float', label: 'float' },
                              { value: 'select', label: 'select' }
                            ]}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={9}>
                        <Form.Item noStyle shouldUpdate>
                          {() => {
                            const rowType = form.getFieldValue(['param_space', f.name, 'type'])
                            if (rowType === 'select') {
                              return (
                                <Form.Item
                                  name={[f.name, 'choices']}
                                  style={{ marginBottom: 0 }}
                                  rules={[{ required: true, message: '请输入候选值' }]}
                                >
                                  <Input placeholder="候选值，逗号分隔" disabled={!templateConfig} />
                                </Form.Item>
                              )
                            }
                            return (
                              <Space.Compact style={{ width: '100%' }}>
                                <Form.Item
                                  name={[f.name, 'low']}
                                  style={{ marginBottom: 0, width: '50%' }}
                                  rules={[{ required: true, message: 'low' }]}
                                >
                                  <InputNumber placeholder="low" style={{ width: '100%' }} disabled={!templateConfig} />
                                </Form.Item>
                                <Form.Item
                                  name={[f.name, 'high']}
                                  style={{ marginBottom: 0, width: '50%' }}
                                  rules={[{ required: true, message: 'high' }]}
                                >
                                  <InputNumber placeholder="high" style={{ width: '100%' }} disabled={!templateConfig} />
                                </Form.Item>
                              </Space.Compact>
                            )
                          }}
                        </Form.Item>
                      </Col>
                      <Col span={2} style={{ textAlign: 'center' }}>
                        <Button
                          type="text"
                          danger
                          icon={<MinusCircleOutlined />}
                          onClick={() => remove(f.name)}
                        />
                      </Col>
                    </Row>
                  ))}
                  <Button
                    type="dashed"
                    block
                    icon={<PlusOutlined />}
                    onClick={() => add({ type: 'int' })}
                    disabled={!templateConfig}
                  >
                    添加搜索参数
                  </Button>
                </div>
              )}
            </Form.List>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              参数名来自模板策略的 param_schema 与风控字段；int/float 填 low~high 范围，select 填逗号分隔候选值。
            </Typography.Text>
          </Form.Item>

          <Row>
            <Col flex="auto" />
            <Col>
              <Button type="primary" htmlType="submit" loading={submitting} disabled={!templateConfig}>
                提交寻优
              </Button>
            </Col>
          </Row>
        </Form>
      </Card>

      <Card title="寻优任务列表">
        <Table<OptimizeListItem>
          rowKey="task_id"
          dataSource={list}
          columns={columns}
          loading={loadingList}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
        />
      </Card>
    </Space>
  )
}
