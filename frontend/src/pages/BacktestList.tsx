import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Button,
  Card,
  Col,
  Collapse,
  Divider,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tooltip,
  Typography
} from 'antd'
import { PlayCircleOutlined, SaveOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import {
  createBacktest,
  createTemplate,
  deleteBacktest,
  deleteTemplate,
  errDetail,
  getBacktests,
  getStrategies,
  getTemplates
} from '../api/client'
import type {
  BacktestCreateRequest,
  BacktestListItem,
  BacktestTemplateItem,
  ParamValue,
  RiskConfig,
  Strategy,
  UniverseMeta
} from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import ParamSchemaForm from '../components/ParamSchemaForm'
import RiskConfigForm, { DEFAULT_RISK_CONFIG } from '../components/RiskConfigForm'
import BacktestRangePicker from '../components/BacktestRangePicker'
import StockPicker from '../components/StockPicker'

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
  handling_fee?: number
  regulatory_fee?: number
  warmup_days?: number
  monthly_withdraw_base?: number
  t_profit_withdraw_pct?: number
  min_t_amount?: number
  exclude_st?: boolean
  params?: Record<string, string | number | boolean>
  risk_config?: Record<string, string | number>
  capital_preset?: string
}

/** 资金档预设：选择后一键填充 初始资金 / 最大持股 / 月提取 / 最小T金额 */
const CAPITAL_PRESETS: Record<string, { label: string; initial_capital: number; max_holdings: number; monthly_withdraw_base: number; min_t_amount: number }> = {
  '50w': { label: '50万档', initial_capital: 500000, max_holdings: 3, monthly_withdraw_base: 6000, min_t_amount: 30000 },
  '300w': { label: '300万档', initial_capital: 3000000, max_holdings: 5, monthly_withdraw_base: 20000, min_t_amount: 80000 }
}

export default function BacktestList() {
  const [form] = Form.useForm<BacktestFormValues>()
  const navigate = useNavigate()
  const location = useLocation()
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [strategyId, setStrategyId] = useState<string | null>(null)
  // 条件选股溯源 meta（随 config 存模板/report）
  const [universeMeta, setUniverseMeta] = useState<UniverseMeta | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [list, setList] = useState<BacktestListItem[]>([])
  const [loadingList, setLoadingList] = useState(true)
  // ---- 配置模板 ----
  const [templates, setTemplates] = useState<BacktestTemplateItem[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<number | undefined>(undefined)
  const [saveModalOpen, setSaveModalOpen] = useState(false)
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const [saveSource, setSaveSource] = useState<{ config: BacktestCreateRequest } | null>(null)
  const prefillApplied = useRef(false)

  const strategy = useMemo(() => strategies.find((s) => s.id === strategyId), [strategies, strategyId])

  const loadTemplates = useCallback(async () => {
    try {
      setTemplates(await getTemplates())
    } catch {
      /* 模板加载失败静默 */
    }
  }, [])

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
    loadTemplates()
  }, [loadTemplates])

  useEffect(() => {
    fetchList()
  }, [fetchList])

  /** 表单值 -> 回测配置（模板保存与提交共用；允许字段缺失，模板可存半成品） */
  const buildConfigFromValues = useCallback((values: BacktestFormValues): BacktestCreateRequest => {
    return {
      name: values.name ?? '',
      strategy_id: values.strategy_id,
      params: (values.params ?? {}) as Record<string, ParamValue>,
      risk_config: values.risk_config as RiskConfig | undefined,
      universe: values.universe ?? [],
      universe_meta: universeMeta ?? null,
      start_date: values.dateRange?.[0]?.format('YYYY-MM-DD') ?? '',
      end_date: values.dateRange?.[1]?.format('YYYY-MM-DD') ?? '',
      period: (values.period as 'daily' | 'minute5') ?? 'daily',
      initial_capital: values.initial_capital ?? 400000,
      slippage_pct: values.slippage_pct,
      commission_rate: values.commission_rate,
      commission_min: values.commission_min,
      stamp_tax: values.stamp_tax,
      transfer_fee: values.transfer_fee,
      handling_fee: values.handling_fee,
      regulatory_fee: values.regulatory_fee,
      warmup_days: values.warmup_days,
      monthly_withdraw_base: values.monthly_withdraw_base,
      t_profit_withdraw_pct: values.t_profit_withdraw_pct,
      min_t_amount: values.min_t_amount,
      exclude_st: values.exclude_st ?? true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universeMeta])

  /** 回测配置 -> 表单（载入模板 / AI 建议预填共用） */
  const applyConfigToForm = useCallback(
    (cfg: BacktestCreateRequest, tip = '配置已载入') => {
      setStrategyId(cfg.strategy_id)
      setUniverseMeta(cfg.universe_meta ?? null)
      const values: Record<string, unknown> = {
        name: cfg.name ?? '',
        strategy_id: cfg.strategy_id,
        period: cfg.period,
        params: cfg.params ?? {},
        universe: cfg.universe ?? [],
        initial_capital: cfg.initial_capital ?? 400000,
        risk_config: (cfg.risk_config ?? DEFAULT_RISK_CONFIG) as Record<string, string | number>,
        exclude_st: cfg.exclude_st ?? true
      }
      if (cfg.start_date && cfg.end_date) {
        values.dateRange = [dayjs(cfg.start_date), dayjs(cfg.end_date)]
      }
      const numericKeys = [
        'slippage_pct', 'commission_rate', 'commission_min', 'stamp_tax', 'transfer_fee',
        'handling_fee', 'regulatory_fee', 'warmup_days', 'monthly_withdraw_base',
        't_profit_withdraw_pct', 'min_t_amount'
      ] as const
      numericKeys.forEach((k) => {
        const v = cfg[k]
        if (v !== undefined && v !== null) values[k] = v
      })
      form.setFieldsValue(values as unknown as BacktestFormValues)
      message.success(tip)
    },
    [form]
  )

  // AI 分析页「应用建议」跳转过来时预填表单（只应用一次）
  useEffect(() => {
    const st = location.state as { prefill?: BacktestCreateRequest } | null
    if (!st?.prefill || prefillApplied.current || strategies.length === 0) return
    prefillApplied.current = true
    applyConfigToForm(st.prefill, '已载入 AI 优化配置，确认后可提交下一轮回测')
    // replace 清掉 state，避免刷新重复应用
    navigate('/backtests', { replace: true })
  }, [location.state, strategies, applyConfigToForm, navigate])

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

  // 股票池远程搜索与批量粘贴逻辑已抽至 StockPicker 组件（方案 §8.3）

  const onStrategyChange = (id: string) => {
    setStrategyId(id)
    const s = strategies.find((x) => x.id === id)
    const params: Record<string, string | number | boolean> = {}
    s?.param_schema?.forEach((p) => {
      if (p.default !== undefined) params[p.key] = p.default
    })
    form.setFieldsValue({ params, period: s?.periods?.[0] })
  }

  /** 资金档预设：一键填充 初始资金 / 最大持股 / 月提取 / 最小T金额 */
  const applyCapitalPreset = (presetKey: string) => {
    if (!presetKey) return
    const p = CAPITAL_PRESETS[presetKey]
    if (!p) return
    form.setFieldsValue({
      initial_capital: p.initial_capital,
      monthly_withdraw_base: p.monthly_withdraw_base,
      min_t_amount: p.min_t_amount,
      risk_config: { ...(form.getFieldValue('risk_config') ?? {}), max_holdings: p.max_holdings }
    })
    message.success(`${p.label}已填充：持股≤${p.max_holdings} · 月提取 ${(p.monthly_withdraw_base / 10000).toFixed(1)}万 · 最小T金额 ${(p.min_t_amount / 10000).toFixed(0)}万`)
  }

  const onFinish = async () => {
    // 用 getFieldsValue(true) 取表单 store 全部字段（含折叠面板收起时未挂载的字段），
    // 避免载入模板/默认的出金、费率等配置因面板收起而在提交时丢失（否则后端落到默认值 0）
    const values = form.getFieldsValue(true) as BacktestFormValues
    try {
      const res = await createBacktest(buildConfigFromValues(values))
      message.success('回测任务已创建')
      navigate(`/backtests/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '创建回测失败'))
    }
  }

  // ---- 模板操作 ----

  const onLoadTemplate = () => {
    const t = templates.find((x) => x.id === selectedTemplateId)
    if (!t) return
    applyConfigToForm(t.config, `已载入模板「${t.name}」`)
  }

  const onDeleteTemplate = async () => {
    if (!selectedTemplateId) return
    try {
      await deleteTemplate(selectedTemplateId)
      message.success('模板已删除')
      setSelectedTemplateId(undefined)
      loadTemplates()
    } catch (err) {
      message.error(errDetail(err, '删除模板失败'))
    }
  }

  /** 打开保存弹窗：source 为空表示保存当前表单 */
  const openSaveModal = (source?: { config: BacktestCreateRequest }) => {
    if (source) {
      setSaveSource(source)
      setTemplateName(source.config.name || '')
    } else {
      const values = form.getFieldsValue(true)
      if (!values.strategy_id) {
        message.warning('请先选择策略，再保存配置模板')
        return
      }
      setSaveSource({ config: buildConfigFromValues(values) })
      setTemplateName(values.name || '')
    }
    setSaveModalOpen(true)
  }

  const onDeleteBacktest = async (record: BacktestListItem) => {
    try {
      await deleteBacktest(record.task_id)
      message.success('已删除回测任务')
      fetchList()
    } catch (err) {
      message.error(errDetail(err, '删除失败'))
    }
  }

  const onSaveTemplate = async () => {
    if (!saveSource) return
    const name = templateName.trim()
    if (!name) {
      message.warning('请输入模板名')
      return
    }
    setSavingTemplate(true)
    try {
      // 模板名与配置内 name 同步：载入模板时回显的应是当前模板名，
      // 而非保存前记录/表单里的旧任务名（如从列表「存为模板」改名后残留）
      await createTemplate({ name, config: { ...saveSource.config, name } })
      message.success('模板已保存')
      setSaveModalOpen(false)
      loadTemplates()
    } catch (err) {
      message.error(errDetail(err, '保存模板失败'))
    } finally {
      setSavingTemplate(false)
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
      width: 260,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" onClick={() => navigate(`/backtests/${record.task_id}`)}>
            查看
          </Button>
          {record.config && (
            <Button
              type="link"
              size="small"
              onClick={() => openSaveModal({ config: record.config as BacktestCreateRequest })}
            >
              存为模板
            </Button>
          )}
          {record.status === 'failed' && record.error && (
            <Tooltip title={record.error}>
              <Button type="link" size="small" danger>
                失败原因
              </Button>
            </Tooltip>
          )}
          <Popconfirm
            title="删除回测任务"
            description={`将删除「${record.name}」及其报告、关联的 AI 分析，不可恢复`}
            onConfirm={() => onDeleteBacktest(record)}
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
          >
            <Button
              type="link"
              size="small"
              danger
              disabled={record.status === 'pending' || record.status === 'running'}
            >
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
        title="新建回测"
        extra={
          <Space size="small" wrap>
            <Select
              value={selectedTemplateId}
              onChange={setSelectedTemplateId}
              placeholder="选择配置模板"
              style={{ width: 200 }}
              showSearch
              optionFilterProp="label"
              options={templates.map((t) => ({
                value: t.id,
                label: t.name
              }))}
            />
            <Button size="small" disabled={!selectedTemplateId} onClick={onLoadTemplate}>
              载入
            </Button>
            <Popconfirm
              title="删除该配置模板？"
              disabled={!selectedTemplateId}
              onConfirm={onDeleteTemplate}
            >
              <Button size="small" danger disabled={!selectedTemplateId}>
                删除
              </Button>
            </Popconfirm>
            <Divider type="vertical" />
            <Button size="small" icon={<SaveOutlined />} onClick={() => openSaveModal()}>
              保存当前为模板
            </Button>
          </Space>
        }
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{
            initial_capital: 400000,
            slippage_pct: 0.001,
            commission_rate: 0.00005,
            commission_min: 5,
            stamp_tax: 0.0005,
            transfer_fee: 0.00001,
            handling_fee: 0.0000341,
            regulatory_fee: 0.00002,
            warmup_days: 0,
            monthly_withdraw_base: 5000,
            t_profit_withdraw_pct: 10,
            min_t_amount: 20000,
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
                label="股票池（手动选择 / 条件选股）"
                rules={[{ required: true, message: '请选择至少一只股票' }]}
              >
                <StockPicker
                  meta={universeMeta}
                  onMetaChange={(m) => setUniverseMeta(m ?? null)}
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="dateRange"
                label="回测区间"
                rules={[{ required: true, message: '请选择时间区间' }]}
              >
                <BacktestRangePicker />
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
              <Form.Item
                name="capital_preset"
                label="资金档预设"
                extra="一键填充初始资金/最大持股/月提取/最小T金额"
              >
                <Select
                  placeholder="选择资金档"
                  allowClear
                  options={Object.entries(CAPITAL_PRESETS).map(([k, p]) => ({
                    value: k,
                    label: p.label
                  }))}
                  onChange={applyCapitalPreset}
                />
              </Form.Item>
            </Col>
          </Row>

          <Collapse
            style={{ marginBottom: 16 }}
            defaultActiveKey={['cost', 'withdraw', 'risk']}
            items={[
              {
                key: 'cost',
                label: '交易成本（默认按现行官方费率：佣金万0.5最低5元，印花税万5卖出，经手/证管/过户双边）',
                children: (
                  <Row gutter={16}>
                    <Col span={4}>
                      <Form.Item name="slippage_pct" label="滑点比例" extra="0.001 表示 0.1%">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0005} />
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      <Form.Item name="commission_rate" label="佣金率" extra="0.00005 = 万0.5">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.00001} />
                      </Form.Item>
                    </Col>
                    <Col span={3}>
                      <Form.Item name="commission_min" label="最低佣金（元）">
                        <InputNumber style={{ width: '100%' }} min={0} step={1} />
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      <Form.Item name="stamp_tax" label="印花税" extra="0.0005 = 万5，仅卖出">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0001} />
                      </Form.Item>
                    </Col>
                    <Col span={3}>
                      <Form.Item name="handling_fee" label="经手费" extra="万0.341 双边">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0000034} />
                      </Form.Item>
                    </Col>
                    <Col span={3}>
                      <Form.Item name="regulatory_fee" label="证管费" extra="万0.2 双边">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.000002} />
                      </Form.Item>
                    </Col>
                    <Col span={3}>
                      <Form.Item name="transfer_fee" label="过户费" extra="万0.1 双边">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.000005} />
                      </Form.Item>
                    </Col>
                  </Row>
                )
              },
              {
                key: 'withdraw',
                label: '账户与月度出金（逐笔T盈利提成 + 月末兜底；0 关闭）',
                children: (
                  <Row gutter={16}>
                    <Col span={6}>
                      <Form.Item
                        name="monthly_withdraw_base"
                        label="每月提取目标额（元）"
                        extra="月中已达标则月末不再提取；0=关闭"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} step={500} />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        name="t_profit_withdraw_pct"
                        label="T盈利提成（%）"
                        extra="每笔做T盈利即时提取比例"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} max={100} step={1} />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        name="min_t_amount"
                        label="最小T金额（元）"
                        extra="低于该金额的做T自动跳过（防碎单费用磨损）"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} step={5000} />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        name="warmup_days"
                        label="指标预热（交易日）"
                        extra="0=自动按策略建议前推；数据不足时按实际"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} step={10} />
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

      <Modal
        title="保存配置模板"
        open={saveModalOpen}
        onOk={onSaveTemplate}
        onCancel={() => setSaveModalOpen(false)}
        confirmLoading={savingTemplate}
        okText="保存"
        destroyOnClose
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          模板保存后可在「新建回测」右上角快速载入，无需重复配置。
        </Typography.Paragraph>
        <Input
          placeholder="模板名称，例如：双均线-浦发-标准配置"
          value={templateName}
          onChange={(e) => setTemplateName(e.target.value)}
          maxLength={50}
          onPressEnter={onSaveTemplate}
          autoFocus
        />
      </Modal>
    </Space>
  )
}
