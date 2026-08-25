import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button,
  Card,
  Col,
  Collapse,
  DatePicker,
  Form,
  Input,
  InputNumber,
  message,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tooltip,
  Typography
} from 'antd'
import { PlayCircleOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { createBacktest, errDetail, getBacktests, getStocks, getStrategies } from '../api/client'
import type { BacktestListItem, StockItem, Strategy } from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import ParamSchemaForm from '../components/ParamSchemaForm'
import RiskConfigForm, { DEFAULT_RISK_CONFIG } from '../components/RiskConfigForm'

const { RangePicker } = DatePicker

interface BacktestFormValues {
  name: string
  strategy_id: string
  period: string
  universe: string[]
  dateRange: [Dayjs, Dayjs]
  initial_capital: number
  slippage_pct?: number
  commission_rate?: number
  commission_min?: number
  stamp_tax?: number
  transfer_fee?: number
  exclude_st?: boolean
  params?: Record<string, string | number | boolean>
  risk_config?: Record<string, string | number>
}

export default function BacktestList() {
  const [form] = Form.useForm<BacktestFormValues>()
  const navigate = useNavigate()
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [strategyId, setStrategyId] = useState<string | null>(null)
  const [stocks, setStocks] = useState<StockItem[]>([])
  const [stockSearching, setStockSearching] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [list, setList] = useState<BacktestListItem[]>([])
  const [loadingList, setLoadingList] = useState(true)
  const searchTimer = useRef<number | null>(null)

  const strategy = useMemo(() => strategies.find((s) => s.id === strategyId), [strategies, strategyId])

  const fetchList = useCallback(async () => {
    try {
      setList(await getBacktests())
    } catch {
      /* 列表加载失败静默，轮询时继续尝试 */
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => {
    getStrategies()
      .then(setStrategies)
      .catch((err) => message.error(errDetail(err, '策略列表加载失败')))
  }, [])

  useEffect(() => {
    fetchList()
  }, [fetchList])

  // 存在运行中任务时每 3s 自动刷新
  const hasActive = list.some((t) => t.status === 'pending' || t.status === 'running')
  useEffect(() => {
    if (!hasActive) return
    const timer = window.setInterval(() => {
      getBacktests()
        .then(setList)
        .catch(() => {})
    }, 3000)
    return () => window.clearInterval(timer)
  }, [hasActive])

  // 股票池远程搜索（防抖 300ms）
  const onStockSearch = (kw: string) => {
    if (searchTimer.current !== null) {
      window.clearTimeout(searchTimer.current)
    }
    if (!kw) {
      setStocks([])
      return
    }
    searchTimer.current = window.setTimeout(async () => {
      setStockSearching(true)
      try {
        setStocks(await getStocks(kw, 20))
      } catch {
        /* ignore */
      } finally {
        setStockSearching(false)
      }
    }, 300)
  }

  const onStrategyChange = (id: string) => {
    setStrategyId(id)
    const s = strategies.find((x) => x.id === id)
    const params: Record<string, string | number | boolean> = {}
    s?.param_schema?.forEach((p) => {
      if (p.default !== undefined) params[p.key] = p.default
    })
    form.setFieldsValue({ params, period: s?.periods?.[0] })
  }

  const onFinish = async (values: BacktestFormValues) => {
    const [start, end] = values.dateRange
    try {
      const res = await createBacktest({
        name: values.name,
        strategy_id: values.strategy_id,
        params: (values.params ?? {}) as Record<string, string | number | boolean>,
        risk_config: values.risk_config as Record<string, string | number> | undefined,
        universe: values.universe ?? [],
        start_date: start.format('YYYY-MM-DD'),
        end_date: end.format('YYYY-MM-DD'),
        period: (values.period as 'daily' | 'minute5') ?? 'daily',
        initial_capital: values.initial_capital,
        slippage_pct: values.slippage_pct,
        commission_rate: values.commission_rate,
        commission_min: values.commission_min,
        stamp_tax: values.stamp_tax,
        transfer_fee: values.transfer_fee,
        exclude_st: values.exclude_st ?? true
      })
      message.success('回测任务已创建')
      navigate(`/backtests/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建回测失败'))
    }
  }

  const periodOptions = useMemo(() => {
    const all = [
      { value: 'daily', label: '日线' },
      { value: 'minute5', label: '5分钟' }
    ]
    if (!strategy) return all
    return all.filter((o) => strategy.periods.includes(o.value))
  }, [strategy])

  const columns: ColumnsType<BacktestListItem> = [
    { title: '名称', dataIndex: 'name', ellipsis: true },
    { title: '策略', dataIndex: 'strategy_id', width: 110 },
    {
      title: '周期',
      dataIndex: 'period',
      width: 80,
      render: (v: string) => (v === 'daily' ? '日线' : v === 'minute5' ? '5分钟' : v)
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (v: BacktestListItem['status']) => <TaskStatusTag status={v} />
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-')
    },
    {
      title: '操作',
      width: 140,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" onClick={() => navigate(`/backtests/${record.task_id}`)}>
            查看
          </Button>
          {record.status === 'failed' && record.error && (
            <Tooltip title={record.error}>
              <Button type="link" size="small" danger>
                失败原因
              </Button>
            </Tooltip>
          )}
        </Space>
      )
    }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card title="新建回测">
        <Form
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{
            initial_capital: 1000000,
            slippage_pct: 0.001,
            commission_rate: 0.0003,
            commission_min: 5,
            stamp_tax: 0.001,
            transfer_fee: 0.00001,
            exclude_st: true,
            risk_config: DEFAULT_RISK_CONFIG as Record<string, string | number>
          }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请输入任务名称' }]}>
                <Input placeholder="例如：双均线-浦发银行" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="strategy_id" label="策略" rules={[{ required: true, message: '请选择策略' }]}>
                <Select
                  placeholder="请选择策略"
                  options={strategies.map((s) => ({ value: s.id, label: `${s.name}（${s.id}）` }))}
                  onChange={onStrategyChange}
                  showSearch
                  optionFilterProp="label"
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="period" label="回测周期" rules={[{ required: true, message: '请选择周期' }]}>
                <Radio.Group options={periodOptions} optionType="button" />
              </Form.Item>
            </Col>
          </Row>

          {strategy && (
            <Card
              type="inner"
              title={`策略参数 · ${strategy.name}`}
              extra={<Typography.Text type="secondary">{strategy.description}</Typography.Text>}
              style={{ marginBottom: 16 }}
            >
              <Row gutter={16}>
                <ParamSchemaForm schema={strategy.param_schema ?? []} />
              </Row>
            </Card>
          )}

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="universe"
                label="股票池"
                rules={[{ required: true, message: '请选择至少一只股票' }]}
              >
                <Select
                  mode="multiple"
                  placeholder="输入代码或名称搜索"
                  filterOption={false}
                  onSearch={onStockSearch}
                  notFoundContent={stockSearching ? <Spin size="small" /> : null}
                  options={stocks.map((s) => ({
                    value: s.code,
                    label: `${s.code} ${s.name}${s.st ? ' (ST)' : ''}`
                  }))}
                  allowClear
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="dateRange"
                label="回测区间"
                rules={[{ required: true, message: '请选择时间区间' }]}
              >
                <RangePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="initial_capital"
                label="初始资金（元）"
                rules={[{ required: true, message: '请输入初始资金' }]}
              >
                <InputNumber min={1000} step={100000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Collapse
            style={{ marginBottom: 16 }}
            items={[
              {
                key: 'cost',
                label: '交易成本',
                children: (
                  <Row gutter={16}>
                    <Col span={5}>
                      <Form.Item name="slippage_pct" label="滑点比例" extra="0.001 表示 0.1%">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0005} />
                      </Form.Item>
                    </Col>
                    <Col span={5}>
                      <Form.Item name="commission_rate" label="佣金率" extra="0.0003 = 万3">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0001} />
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      <Form.Item name="commission_min" label="最低佣金（元）">
                        <InputNumber style={{ width: '100%' }} min={0} step={1} />
                      </Form.Item>
                    </Col>
                    <Col span={5}>
                      <Form.Item name="stamp_tax" label="印花税" extra="0.001 = 千1">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0005} />
                      </Form.Item>
                    </Col>
                    <Col span={5}>
                      <Form.Item name="transfer_fee" label="过户费" extra="0.00001 = 十万分一">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.00001} />
                      </Form.Item>
                    </Col>
                  </Row>
                )
              },
              {
                key: 'risk',
                label: '风控配置',
                children: <RiskConfigForm />
              }
            ]}
          />

          <Row gutter={16} align="middle">
            <Col>
              <Form.Item name="exclude_st" label="剔除ST" valuePropName="checked" style={{ marginBottom: 0 }}>
                <Switch />
              </Form.Item>
            </Col>
            <Col flex="auto" />
            <Col>
              <Button type="primary" htmlType="submit" loading={submitting} icon={<PlayCircleOutlined />}>
                提交回测
              </Button>
            </Col>
          </Row>
        </Form>
      </Card>

      <Card title="回测任务列表">
        <Table<BacktestListItem>
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
