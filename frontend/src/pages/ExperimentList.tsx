import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Form,
  Input,
  InputNumber,
  message,
  Popconfirm,
  Radio,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import {
  createExperiment,
  deleteExperiment,
  errDetail,
  getBacktestReport,
  getBacktests,
  getExperimentList,
  getStrategies
} from '../api/client'
import type {
  BacktestCreateRequest,
  BacktestListItem,
  ExperimentCell,
  ExperimentListItem,
  ExperimentMatrix,
  Strategy,
  UniverseMeta
} from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import BacktestRangePicker from '../components/BacktestRangePicker'
import StockPicker from '../components/StockPicker'

/** 实验矩阵：cell -> (时钟, T开关) 与标签 */
const CELLS: Array<{ value: ExperimentCell; label: string; desc: string }> = [
  { value: 'A', label: 'A · 日线时钟×T', desc: '趋势信号每日末bar评估，次日开盘成交；做T开' },
  { value: 'B', label: 'B · 盘中时钟×T', desc: '现状：盘中触发趋势 + 做T' },
  { value: 'C', label: 'C · 日线时钟×无T', desc: '日线时钟 + 做T关（max_t_times=0）' },
  { value: 'D', label: 'D · 盘中时钟×无T', desc: '盘中时钟 + 做T关（max_t_times=0）' }
]

/** t_mode 机制矩阵（T_REFACTOR L3）：cell -> 做T机制 */
const TMODE_CELLS: Array<{ value: ExperimentCell; label: string; desc: string }> = [
  { value: 'A', label: 'A · 网格+双止损（L1）', desc: '现有网格做T + 债务时间/价格双止损封右尾' },
  { value: 'B', label: 'B · 回补纪律（L2）', desc: '价格回落到卖出价下方才限价回补，拒绝追高' },
  { value: 'C', label: 'C · 无T基线', desc: '做T关闭，纯趋势持仓对照' },
  { value: 'D', label: 'D · 时点规律T', desc: '每日 09:35 高抛 1/4 底仓、14:50 尾盘买回' }
]

const MATRIX_OPTIONS: Array<{ value: ExperimentMatrix; label: string }> = [
  { value: 'clock', label: '时钟 × 做T（2×2）' },
  { value: 't_mode', label: '做T四机制竞争（L3）' }
]

const CAPITAL_PRESETS = [
  { value: 400_000, label: '40万' },
  { value: 3_000_000, label: '300万' }
]

interface ExperimentFormValues {
  name: string
  template?: string
  matrix: ExperimentMatrix
  cells: ExperimentCell[]
  capitals: number[]
  custom_capital?: number | null
  range?: [dayjs.Dayjs, dayjs.Dayjs] | null
  with_e?: boolean
}

export default function ExperimentList() {
  const [form] = Form.useForm<ExperimentFormValues>()
  const matrixWatch = Form.useWatch('matrix', form)
  const navigate = useNavigate()
  const [backtests, setBacktests] = useState<BacktestListItem[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [templateConfig, setTemplateConfig] = useState<BacktestCreateRequest | null>(null)
  // 条件选股覆盖股票池（可选）：非空时覆盖模板基座的 universe / universe_meta
  const [universeOverride, setUniverseOverride] = useState<{
    codes: string[]
    meta?: UniverseMeta | null
  }>({ codes: [] })
  const [submitting, setSubmitting] = useState(false)
  const [list, setList] = useState<ExperimentListItem[]>([])
  const [loadingList, setLoadingList] = useState(true)

  const fetchList = useCallback(async () => {
    try {
      setList(await getExperimentList())
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

  const hasActive = list.some((e) => e.status === 'pending' || e.status === 'running')
  useEffect(() => {
    if (!hasActive) return
    const timer = window.setInterval(() => {
      getExperimentList()
        .then(setList)
        .catch(() => {})
    }, 5000)
    return () => window.clearInterval(timer)
  }, [hasActive])

  const momentumBacktests = backtests.filter(
    (b) => b.status === 'success' && b.strategy_id === 'momentum_t'
  )

  const onTemplateChange = async (taskId: string) => {
    setTemplateConfig(null)
    setUniverseOverride({ codes: [] })
    if (!taskId) return
    try {
      const report = await getBacktestReport(taskId)
      const cfg = report.config
      setTemplateConfig(cfg)
      const curName = form.getFieldValue('name')
      if (!curName) {
        form.setFieldsValue({ name: `${report.name}-对比实验` })
      }
      if (cfg.start_date && cfg.end_date) {
        form.setFieldsValue({ range: [dayjs(cfg.start_date), dayjs(cfg.end_date)] })
      }
    } catch (err) {
      message.error(errDetail(err, '读取回测配置失败'))
    }
  }

  const onFinish = async (values: ExperimentFormValues) => {
    if (!templateConfig) {
      message.warning('请先选择 momentum_t 模板回测')
      return
    }
    if (!values.cells?.length) {
      message.warning('请至少勾选一个实验格')
      return
    }
    const capitals = [...(values.capitals ?? [])]
    if (values.custom_capital && values.custom_capital > 0) {
      capitals.push(values.custom_capital)
    }
    if (!capitals.length) {
      message.warning('请至少选择一个资金档')
      return
    }
    const [start, end] = values.range ?? []
    if (!start || !end) {
      message.warning('请选择回测区间')
      return
    }
    setSubmitting(true)
    try {
      // 条件选股覆盖：非空时用其替换模板基座的股票池（并携带溯源 meta）
      const base = { ...templateConfig }
      if (universeOverride.codes.length > 0) {
        base.universe = universeOverride.codes
        base.universe_meta = universeOverride.meta ?? null
      }
      const res = await createExperiment({
        name: values.name,
        base_config: base,
        cells: values.cells,
        capitals,
        start_date: start.format('YYYY-MM-DD'),
        end_date: end.format('YYYY-MM-DD'),
        with_e: values.matrix === 't_mode' ? false : (values.with_e ?? false),
        matrix: values.matrix ?? 'clock'
      })
      message.success(`实验已创建，共 ${res.sub_task_ids.length} 个子回测任务`)
      navigate(`/experiments/${res.experiment_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建实验失败'))
    } finally {
      setSubmitting(false)
    }
  }

  const onDelete = async (expId: string) => {
    try {
      await deleteExperiment(expId)
      message.success('实验已删除（含子任务）')
      fetchList()
    } catch (err) {
      message.error(errDetail(err, '删除失败'))
    }
  }

  const columns: ColumnsType<ExperimentListItem> = [
    { title: '实验名', dataIndex: 'name', ellipsis: true },
    {
      title: '矩阵',
      dataIndex: 'matrix_type',
      width: 110,
      render: (v?: ExperimentMatrix) => (
        <Tag color={v === 't_mode' ? 'geekblue' : 'default'}>
          {v === 't_mode' ? '做T四机制' : '时钟×做T'}
        </Tag>
      )
    },
    {
      title: '格',
      dataIndex: 'cells',
      width: 120,
      render: (c: ExperimentCell[]) => c.join(' / ')
    },
    {
      title: '资金档',
      dataIndex: 'capitals',
      width: 140,
      render: (c: number[]) => c.map((x) => `${x / 10000}万`).join(' / ')
    },
    {
      title: '子任务',
      dataIndex: 'sub_count',
      width: 80,
      render: (n: number) => `${n}`
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v: ExperimentListItem['status']) => <TaskStatusTag status={v} />
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 90,
      render: (v: number) => `${v}%`
    },
    { title: '创建时间', dataIndex: 'created_at', width: 170 },
    {
      title: '操作',
      width: 130,
      render: (_, record) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => navigate(`/experiments/${record.experiment_id}`)}>
            查看
          </Button>
          <Popconfirm title="删除该实验及其全部子回测？" onConfirm={() => onDelete(record.experiment_id)}>
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      )
    }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title={
          matrixWatch === 't_mode'
            ? '新建机制竞争实验（做T四机制 × 资金档）'
            : '新建对比实验（趋势×做T 2×2 矩阵 × 资金档）'
        }
      >
        <Form<ExperimentFormValues>
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{
            matrix: 'clock',
            cells: ['A', 'B', 'C', 'D'],
            capitals: [400_000, 3_000_000]
          }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="name" label="实验名称" rules={[{ required: true, message: '请输入实验名称' }]}>
                <Input placeholder="例如：趋势×做T 对比" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item
                name="template"
                label="模板回测（取其配置，需 momentum_t）"
                rules={[{ required: true, message: '请选择模板回测' }]}
              >
                <Select
                  placeholder="选择已成功的 momentum_t 回测作为基座配置"
                  showSearch
                  optionFilterProp="label"
                  options={momentumBacktests.map((b) => ({
                    value: b.task_id,
                    label: `${b.name}（${b.task_id}）`
                  }))}
                  onChange={onTemplateChange}
                  allowClear
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="range" label="回测区间">
                <BacktestRangePicker />
              </Form.Item>
            </Col>
          </Row>

          {templateConfig && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={`基座配置：策略 ${templateConfig.strategy_id} · 周期 ${
                templateConfig.period
              } · 股票池 ${templateConfig.universe?.length ?? 0} 只`}
            />
          )}

          {templateConfig && (
            <Collapse
              ghost
              style={{ marginBottom: 8 }}
              items={[
                {
                  key: 'override',
                  label: (
                    <Typography.Text type="secondary">
                      条件选股覆盖股票池（可选，用筛选/随机池替换基座 universe）
                    </Typography.Text>
                  ),
                  children: (
                    <StockPicker
                      value={universeOverride.codes}
                      onChange={(c) => setUniverseOverride((prev) => ({ codes: c, meta: prev.meta }))}
                      meta={universeOverride.meta}
                      onMetaChange={(m) => setUniverseOverride((prev) => ({ codes: prev.codes, meta: m }))}
                    />
                  )
                }
              ]}
            />
          )}

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="matrix"
                label="实验矩阵类型"
                rules={[{ required: true, message: '请选择矩阵类型' }]}
              >
                <Radio.Group
                  optionType="button"
                  buttonStyle="solid"
                  options={MATRIX_OPTIONS}
                  onChange={(e) => {
                    const m = e.target.value as ExperimentMatrix
                    form.setFieldsValue({
                      cells: ['A', 'B', 'C', 'D'],
                      ...(m === 't_mode' ? { with_e: false } : {})
                    })
                  }}
                />
              </Form.Item>
              <Form.Item
                name="cells"
                label={matrixWatch === 't_mode' ? '机制格（做T四机制）' : '实验矩阵（时钟 × 做T）'}
              >
                <Checkbox.Group style={{ width: '100%' }}>
                  <Space direction="vertical">
                    {(matrixWatch === 't_mode' ? TMODE_CELLS : CELLS).map((c) => (
                      <Checkbox key={c.value} value={c.value}>
                        <b>{c.label}</b>
                        <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                          {c.desc}
                        </Typography.Text>
                      </Checkbox>
                    ))}
                  </Space>
                </Checkbox.Group>
              </Form.Item>
              {matrixWatch !== 't_mode' && (
                <Form.Item name="with_e" valuePropName="checked" style={{ marginTop: -8 }}>
                  <Checkbox>
                    <b>E · 纯日线15年参考</b>
                    <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                      单独子回测（period=daily，2010 起，仅趋势层），不进矩阵归因，跑得较久
                    </Typography.Text>
                  </Checkbox>
                </Form.Item>
              )}
            </Col>
            <Col span={6}>
              <Form.Item name="capitals" label="资金档（预设）">
                <Checkbox.Group options={CAPITAL_PRESETS} />
              </Form.Item>
              <Form.Item name="custom_capital" label="自定义资金档（元，可选）">
                <InputNumber min={10000} step={100000} style={{ width: '100%' }} placeholder="如 1000000" />
              </Form.Item>
            </Col>
            <Col span={6} style={{ display: 'flex', alignItems: 'flex-end' }}>
              <Button type="primary" htmlType="submit" loading={submitting} block>
                创建实验
              </Button>
            </Col>
          </Row>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
            将创建 「勾选格数 × 资金档数」 个子回测任务（进程池顺序执行，单个失败不影响其它格）。
          </Typography.Paragraph>
        </Form>
      </Card>

      <Card title="对比实验列表">
        <Table
          rowKey="experiment_id"
          loading={loadingList}
          columns={columns}
          dataSource={list}
          pagination={false}
          size="small"
        />
      </Card>
    </Space>
  )
}
