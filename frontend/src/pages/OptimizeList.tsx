import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
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
import {
  ThunderboltOutlined,
  MinusCircleOutlined,
  PlusOutlined
} from '@ant-design/icons'
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
  OptimizeCreateRequest,
  OptimizeGroupInput,
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
  step?: number
  choices?: string
}

interface OptimizeFormValues {
  name: string
  template?: string
  n_trials: number
  rounds: number
  metric: 'annual_return' | 'sharpe' | 'calmar' | 'total_return'
  objective?: {
    n_windows: number
    variance_penalty: number
    dd_floor?: number | null
  }
  param_space?: SpaceRow[]
  groups?: Array<{ name: string; n_trials: number; params?: SpaceRow[] }>
}

/** momentum_t 预设：5 组分层的搜索空间（docs/OPTIMIZE_AND_AI_PLAN.md 方案A A1.1） */
const MOMENTUM_T_PRESET_GROUPS: Array<{ name: string; n_trials: number; params: SpaceRow[] }> = [
  {
    name: '趋势层', n_trials: 40,
    params: [
      { key: 'trend_ma', type: 'int', low: 30, high: 90, step: 5 },
      { key: 'slope_n', type: 'int', low: 3, high: 8 }
    ]
  },
  {
    name: '仓位与选股', n_trials: 40,
    params: [
      { key: 'top_n', type: 'int', low: 2, high: 5 },
      { key: 'base_pct_max', type: 'float', low: 40, high: 90, step: 5 }
    ]
  },
  {
    name: '加仓与过热', n_trials: 30,
    params: [
      { key: 'max_adds', type: 'int', low: 0, high: 3 },
      { key: 'add_breakout_n', type: 'int', low: 10, high: 40 },
      { key: 'overheat_k', type: 'float', low: 2, high: 4, step: 0.5 }
    ]
  },
  {
    name: '做T网格', n_trials: 40,
    params: [
      { key: 'grid_atr_mult', type: 'float', low: 0.3, high: 1.0, step: 0.1 },
      { key: 't_ratio_base', type: 'float', low: 15, high: 40, step: 5 },
      { key: 'max_t_times', type: 'int', low: 2, high: 6 }
    ]
  },
  {
    name: '风控', n_trials: 30,
    params: [
      { key: 'atr_multiplier', type: 'float', low: 1.0, high: 2.5, step: 0.25 },
      { key: 'trailing_stop_pct', type: 'float', low: 3, high: 8, step: 1 },
      // 单票仓位上限收窄到 30~70%，防止寻优选中 >70% 的激进暴露（审计 B7）
      { key: 'max_position_pct_per_stock', type: 'float', low: 30, high: 70, step: 5 }
    ]
  }
]

/** 分组下拉项：label 为组名，options 为组内参数 */
interface GroupedOption {
  label: string
  options: Array<{ value: string; label: string }>
}

/** 参数搜索空间行编辑器（平铺 / 组内嵌套共用） */
function ParamRows({
  parent,
  options,
  disabled
}: {
  parent: string | Array<string | number>
  options: GroupedOption[]
  disabled: boolean
}) {
  const form = Form.useFormInstance()
  return (
    <Form.List name={parent as never}>
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
                    options={options}
                    showSearch
                    // 分组模式下不能用 optionFilterProp（只会匹配组名），按叶子项 label 过滤
                    filterOption={(input, option) =>
                      String(option?.label ?? '').toLowerCase().includes(input.toLowerCase())
                    }
                    disabled={disabled}
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
                    const rowType = form.getFieldValue([...(parent as unknown[]), f.name, 'type'])
                    if (rowType === 'select') {
                      return (
                        <Form.Item
                          name={[f.name, 'choices']}
                          style={{ marginBottom: 0 }}
                          rules={[{ required: true, message: '请输入候选值' }]}
                        >
                          <Input placeholder="候选值，逗号分隔" disabled={disabled} />
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
                          <InputNumber placeholder="low" style={{ width: '100%' }} disabled={disabled} />
                        </Form.Item>
                        <Form.Item
                          name={[f.name, 'high']}
                          style={{ marginBottom: 0, width: '50%' }}
                          rules={[{ required: true, message: 'high' }]}
                        >
                          <InputNumber placeholder="high" style={{ width: '100%' }} disabled={disabled} />
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
            disabled={disabled}
          >
            添加搜索参数
          </Button>
        </div>
      )}
    </Form.List>
  )
}

function buildSpace(rows?: SpaceRow[]): Record<string, ParamSpaceItem> {
  const space: Record<string, ParamSpaceItem> = {}
  for (const row of rows ?? []) {
    if (!row?.key) continue
    if (row.type === 'select') {
      const choices = String(row.choices ?? '')
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean)
      space[row.key] = { type: 'select', choices }
    } else {
      space[row.key] = { type: row.type, low: row.low, high: row.high, step: row.step }
    }
  }
  return space
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
  const [mode, setMode] = useState<'flat' | 'grouped'>('flat')

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

  const paramOptions = useMemo<GroupedOption[]>(() => {
    if (!templateConfig) return []
    const s = strategies.find((x) => x.id === templateConfig.strategy_id)
    const bucket = (
      items: Array<{ key: string; label: string; group?: string }>,
      prefix: string
    ): GroupedOption[] => {
      const map = new Map<string, Array<{ value: string; label: string }>>()
      for (const it of items) {
        const g = it.group || '其他'
        if (!map.has(g)) map.set(g, [])
        map.get(g)!.push({ value: it.key, label: `${it.label}（${it.key}）` })
      }
      return [...map].map(([g, options]) => ({ label: `${prefix} · ${g}`, options }))
    }
    return [
      ...bucket(s?.param_schema ?? [], '策略参数'),
      ...bucket(RISK_FIELDS, '风控')
    ]
  }, [templateConfig, strategies])

  const applyMomentumPreset = () => {
    if (!templateConfig || templateConfig.strategy_id !== 'momentum_t') {
      message.warning('预设仅适用于 momentum_t 策略，请先选择 momentum_t 回测模板')
      return
    }
    form.setFieldsValue({ groups: MOMENTUM_T_PRESET_GROUPS })
    setMode('grouped')
    message.success('已载入 momentum_t 5 组分层次优预设')
  }

  const onFinish = async (values: OptimizeFormValues) => {
    if (!templateConfig) {
      message.warning('请先选择模板回测')
      return
    }
    const req: OptimizeCreateRequest = {
      name: values.name,
      backtest_config: { ...templateConfig },
      metric: values.metric ?? 'annual_return'
    }
    if (mode === 'grouped') {
      // 分组坐标轮换
      const groups: OptimizeGroupInput[] = []
      let total = 0
      for (const g of values.groups ?? []) {
        const space = buildSpace(g.params)
        if (!g.name?.trim() || !space) {
          message.warning('每组都需要填写组名和至少一个搜索参数')
          return
        }
        total += g.n_trials
        groups.push({ name: g.name.trim(), n_trials: g.n_trials, params: space })
      }
      if (!groups.length) {
        message.warning('请至少配置一个参数组')
        return
      }
      const obj = values.objective
      req.groups = groups
      req.rounds = values.rounds ?? 1
      req.objective = {
        metric: values.metric ?? 'annual_return',
        n_windows: obj?.n_windows ?? 3,
        variance_penalty: obj?.variance_penalty ?? 0.5,
        dd_floor: obj?.dd_floor
      }
      req.n_trials = total * req.rounds
      if (req.n_trials > 2000) {
        message.error(`总试验预算 ${req.n_trials} 超过上限 2000（Σ组trials × 轮次）`)
        return
      }
    } else {
      // 平铺模式（向后兼容）
      const space = buildSpace(values.param_space)
      if (Object.keys(space).length === 0) {
        message.warning('请至少配置一个搜索参数')
        return
      }
      req.param_space = space
      req.n_trials = values.n_trials ?? 50
    }
    setSubmitting(true)
    try {
      const res = await createOptimize(req)
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

  const objectivePanel = (
    <Row gutter={16}>
      <Col span={6}>
        <Form.Item name={['objective', 'n_windows']} label="样本内切窗数 n_windows" initialValue={3}>
          <InputNumber min={1} max={20} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name={['objective', 'variance_penalty']} label="跨窗方差惩罚 λ" initialValue={0.5}>
          <InputNumber min={0} max={5} step={0.1} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name={['objective', 'dd_floor']} label="回撤熔断线（任一窗击穿重罚）" initialValue={-0.4}>
          <InputNumber min={-1} max={0} step={0.05} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="rounds" label="坐标轮换轮数 rounds" initialValue={2}>
          <InputNumber min={1} max={10} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card title="新建参数寻优">
        <Form<OptimizeFormValues>
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{ n_trials: 50, metric: 'annual_return', rounds: 2 }}
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
            <Col span={6}>
              <Form.Item label="搜索模式">
                <Radio.Group
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                  optionType="button"
                  buttonStyle="solid"
                  options={[
                    { value: 'flat', label: '平铺' },
                    { value: 'grouped', label: '分层分组' }
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

          <Row gutter={16}>
            <Col span={6}>
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
            {mode === 'flat' && (
              <Col span={4}>
                <Form.Item name="n_trials" label="试验数 n_trials" rules={[{ required: true }]}>
                  <InputNumber min={1} max={1000} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            )}
            <Col flex="auto" />
            {mode === 'grouped' && (
              <Col>
                <Space>
                  <Button
                    icon={<ThunderboltOutlined />}
                    disabled={!templateConfig || templateConfig.strategy_id !== 'momentum_t'}
                    onClick={applyMomentumPreset}
                    title="填充 momentum_t 5 组分层搜索空间（趋势/仓位/加仓/做T/风控）"
                  >
                    载入 momentum_t 预设
                  </Button>
                </Space>
              </Col>
            )}
          </Row>

          {mode === 'grouped' && (
            <Collapse
              style={{ marginBottom: 16 }}
              items={[
                {
                  key: 'objective',
                  label: '高级目标（多窗口稳健目标）',
                  children: objectivePanel
                }
              ]}
            />
          )}

          {mode === 'flat' ? (
            <Form.Item label="参数搜索空间" required style={{ marginBottom: 8 }}>
              <ParamRows parent="param_space" options={paramOptions} disabled={!templateConfig} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                参数名来自模板策略的 param_schema 与风控字段；int/float 填 low~high 范围，select 填逗号分隔候选值。
              </Typography.Text>
            </Form.Item>
          ) : (
            <Form.Item
              label="参数组（坐标轮换：每组独立 Optuna study，其它组固定当前最优）"
              required
              style={{ marginBottom: 8 }}
            >
              <Form.List name="groups">
                {(gfields, { add: addGroup, remove: removeGroup }) => (
                  <div>
                    {gfields.map((gf) => (
                      <Card
                        key={gf.key}
                        size="small"
                        title={`参数组 ${gf.name + 1}`}
                        style={{ marginBottom: 12 }}
                        extra={
                          <Button
                            type="text"
                            danger
                            icon={<MinusCircleOutlined />}
                            onClick={() => removeGroup(gf.name)}
                          />
                        }
                      >
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              name={[gf.name, 'name']}
                              label="组名"
                              rules={[{ required: true, message: '请输入组名' }]}
                            >
                              <Input placeholder="例如：趋势层" disabled={!templateConfig} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item
                              name={[gf.name, 'n_trials']}
                              label="本组试验数"
                              initialValue={20}
                              rules={[{ required: true, message: '请输入试验数' }]}
                            >
                              <InputNumber min={1} max={1000} style={{ width: '100%' }} disabled={!templateConfig} />
                            </Form.Item>
                          </Col>
                        </Row>
                        <Form.Item label="本组搜索参数" required style={{ marginBottom: 0 }}>
                          <ParamRows parent={[gf.name, 'params']} options={paramOptions} disabled={!templateConfig} />
                        </Form.Item>
                      </Card>
                    ))}
                    <Button
                      type="dashed"
                      block
                      icon={<PlusOutlined />}
                      onClick={() => addGroup({ name: '', n_trials: 20, params: [{ type: 'int' }] })}
                      disabled={!templateConfig}
                    >
                      添加参数组
                    </Button>
                  </div>
                )}
              </Form.List>
            </Form.Item>
          )}

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
