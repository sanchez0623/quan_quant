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
  Modal,
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
  PlusOutlined,
  ImportOutlined,
  ExportOutlined
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
    walk_forward_folds?: number
  }
  param_space?: SpaceRow[]
  groups?: Array<{ name: string; n_trials: number; params?: SpaceRow[] }>
}

/** momentum_t 预设（PARAM_FREEZE 收缩版）：仅核心参数（两轮寻优实证敏感项），
 *  冻结参数不再出现；风控键仅 max_holdings 参与 */
const MOMENTUM_T_PRESET_GROUPS: Array<{ name: string; n_trials: number; params: SpaceRow[] }> = [
  {
    name: '趋势与选股', n_trials: 30,
    params: [
      { key: 'mom_short', type: 'int', low: 10, high: 20, step: 5 },
      { key: 'mom_mid', type: 'int', low: 60, high: 90, step: 10 },
      { key: 'w_short', type: 'float', low: 0.3, high: 0.6, step: 0.1 },
      { key: 'w_mid', type: 'float', low: 0.1, high: 0.3, step: 0.1 },
      { key: 'w_accel', type: 'float', low: 0.1, high: 0.5, step: 0.1 }
    ]
  },
  {
    name: '仓位', n_trials: 20,
    params: [
      { key: 'top_n', type: 'int', low: 2, high: 5 },
      { key: 'base_pct_max', type: 'float', low: 30, high: 70, step: 5 }
    ]
  },
  {
    name: '做T网格', n_trials: 30,
    params: [
      { key: 'grid_atr_mult', type: 'float', low: 0.3, high: 1.0, step: 0.1 },
      { key: 'max_t_times', type: 'int', low: 2, high: 6 },
      { key: 't_ratio_base', type: 'float', low: 15, high: 40, step: 5 }
    ]
  }
]

/** momentum_slot 预设（PARAM_FREEZE 收缩版）：4 组仅核心参数（选股/槽位/止损/做T）；
 *  退出组仅 atr_stop_k——exit_need/partial_exit 等已冻结（上轮消融证伪的过拟合源） */
const MOMENTUM_SLOT_PRESET_GROUPS: Array<{ name: string; n_trials: number; params: SpaceRow[] }> = [
  {
    name: '选股排序', n_trials: 25,
    params: [
      { key: 'mom_short', type: 'int', low: 10, high: 20, step: 5 },
      { key: 'mom_mid', type: 'int', low: 60, high: 90, step: 10 },
      { key: 'w_short', type: 'float', low: 0.3, high: 0.6, step: 0.1 },
      { key: 'w_mid', type: 'float', low: 0.1, high: 0.3, step: 0.1 },
      { key: 'w_accel', type: 'float', low: 0.1, high: 0.5, step: 0.1 }
    ]
  },
  {
    name: '槽位与建仓', n_trials: 20,
    params: [
      { key: 'pool_n', type: 'int', low: 8, high: 16, step: 2 },
      { key: 'max_holdings', type: 'int', low: 3, high: 5 },
      { key: 'base_pct_max', type: 'float', low: 25, high: 45, step: 5 }
    ]
  },
  {
    name: '止损', n_trials: 15,
    params: [
      { key: 'atr_stop_k', type: 'float', low: -5, high: -3, step: 0.5 }
    ]
  },
  {
    name: '做T与正向T', n_trials: 20,
    params: [
      { key: 'grid_atr_mult', type: 'float', low: 0.4, high: 1.0, step: 0.2 },
      { key: 'max_t_times', type: 'int', low: 4, high: 8, step: 2 },
      { key: 't_ratio_base', type: 'float', low: 15, high: 30, step: 5 }
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
        // choices 元素支持 "value|中文标签" 展示格式，寻优空间只取 | 前的 value
        .map((s) => s.split('|')[0].trim())
        .filter(Boolean)
      space[row.key] = { type: 'select', choices }
    } else {
      space[row.key] = { type: row.type, low: row.low, high: row.high, step: row.step }
    }
  }
  return space
}

// ---------------- 参数组 JSON 导入/导出 ----------------

interface ParsedGroup {
  name: string
  n_trials: number
  params: SpaceRow[]
}

/** 简化 JSON 的参数值 -> SpaceRow。
 *  [low,high] / [low,high,step]（全整数=int，含小数=float）；
 *  字符串数组=select 候选；对象 {type,low,high,step} 或 {choices:[...]} */
function parseParamValue(key: string, v: unknown): SpaceRow {
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x === 'string')) {
      return { key, type: 'select', choices: v.join(',') }
    }
    if (v.length === 2 || v.length === 3) {
      const nums = v as number[]
      if (!nums.every((n) => typeof n === 'number' && Number.isFinite(n))) {
        throw new Error(`参数 ${key}：数组元素需全为数字或全为字符串`)
      }
      const isInt = nums.every((n) => Number.isInteger(n))
      return {
        key, type: isInt ? 'int' : 'float',
        low: nums[0], high: nums[1],
        step: v.length === 3 ? nums[2] : undefined
      }
    }
    throw new Error(`参数 ${key}：数组需为 [low,high] / [low,high,step] / 候选值列表`)
  }
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    if (Array.isArray(o.choices)) {
      return { key, type: 'select', choices: o.choices.join(',') }
    }
    if (typeof o.low === 'number' && typeof o.high === 'number') {
      const isInt = o.type === 'int'
        || (o.type !== 'float' && Number.isInteger(o.low) && Number.isInteger(o.high))
      return {
        key, type: isInt ? 'int' : 'float', low: o.low, high: o.high,
        step: typeof o.step === 'number' ? o.step : undefined
      }
    }
    throw new Error(`参数 ${key}：对象需为 {type,low,high,step} 或 {choices:[...]}`)
  }
  throw new Error(`参数 ${key}：值需为数组或对象`)
}

/** 解析参数组 JSON -> 组列表。
 *  支持三种顶层形态：
 *  1) 数组 [{name?, n_trials?, params:{...}}]
 *  2) 对象 {组名: {n_trials?, params:{...}}}
 *  3) 单组对象 {参数名: [low,high,step] / [候选] / {...}}（全部 key 均为参数名时）
 *  未知参数名直接拒绝（防止打错字后静默无效）。 */
function parseGroupsJson(text: string, validKeys: Set<string>): ParsedGroup[] {
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    throw new Error('不是合法的 JSON')
  }
  let raw: Array<{ name?: string; n_trials?: number; params?: unknown }> = []
  if (Array.isArray(data)) {
    raw = data as typeof raw
  } else if (data && typeof data === 'object') {
    const entries = Object.entries(data as Record<string, unknown>)
    if (entries.length && entries.every(
      ([, v]) => v && typeof v === 'object' && 'params' in (v as Record<string, unknown>)
    )) {
      raw = entries.map(([name, v]) => ({ name, ...(v as Record<string, unknown>) }))
    } else {
      raw = [{ params: data }]
    }
  } else {
    throw new Error('顶层需为对象或数组')
  }
  if (!raw.length) {
    throw new Error('至少需要一组参数')
  }
  return raw.map((g, i) => {
    if (!g.params || typeof g.params !== 'object' || Array.isArray(g.params)) {
      throw new Error(`第 ${i + 1} 组缺 params 对象`)
    }
    const rows: SpaceRow[] = []
    const bad: string[] = []
    for (const [k, v] of Object.entries(g.params as Record<string, unknown>)) {
      if (!validKeys.has(k)) {
        bad.push(k)
        continue
      }
      rows.push(parseParamValue(k, v))
    }
    if (bad.length) {
      throw new Error(`未知参数名: ${bad.join(', ')}（须为模板策略参数或风控字段）`)
    }
    if (!rows.length) {
      throw new Error(`第 ${i + 1} 组 params 为空`)
    }
    const n = Number(g.n_trials)
    return {
      name: String(g.name ?? '').trim() || `参数组${i + 1}`,
      n_trials: Number.isFinite(n) && n > 0 ? Math.floor(n) : 20,
      params: rows
    }
  })
}

/** 当前表单参数组 -> 简化 JSON（可直接回贴到导入框） */
function groupsToJson(groups?: Array<{ name?: string; n_trials?: number; params?: SpaceRow[] }>): string {
  const out = (groups ?? [])
    .filter((g) => g.name?.trim() || (g.params ?? []).some((r) => r.key))
    .map((g) => ({
      name: g.name?.trim() || undefined,
      n_trials: g.n_trials,
      params: Object.fromEntries(
        (g.params ?? [])
          .filter((r) => r.key)
          .map((r) => {
            if (r.type === 'select') {
              return [r.key, String(r.choices ?? '')
                .split(/[,，]/).map((s) => s.trim()).filter(Boolean)
                .map((s) => s.split('|')[0].trim()).filter(Boolean)]
            }
            return [r.key, r.step != null ? [r.low, r.high, r.step] : [r.low, r.high]]
          })
      )
    }))
  return JSON.stringify(out, null, 2)
}

/** 策略 -> 分层预设映射 */
const PRESET_BY_STRATEGY: Record<string, { label: string; groups: Array<{ name: string; n_trials: number; params: SpaceRow[] }> }> = {
  momentum_t: { label: 'momentum_t 5 组分层预设', groups: MOMENTUM_T_PRESET_GROUPS },
  momentum_slot: { label: 'momentum_slot 4 组分层预设', groups: MOMENTUM_SLOT_PRESET_GROUPS }
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
  // ---- 参数组 JSON 导入/导出 ----
  const [jsonOpen, setJsonOpen] = useState(false)
  const [jsonText, setJsonText] = useState('')
  const [exportOpen, setExportOpen] = useState(false)
  const [exportText, setExportText] = useState('')

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
      let cfg = report.config
      // 动态选股模板的 universe 由引擎运行时生成（config.universe 为空），
      // 寻优依赖静态池：固化为原回测各段实际交易股票的并集
      if (cfg?.universe_auto) {
        const segs = report.auto_segments ?? []
        const pool = [...new Set(segs.flatMap((s) => s.universe ?? []))]
        if (!pool.length) {
          message.error('该模板为动态选股回测，但报告中缺少段池信息，无法作为寻优模板')
          return
        }
        // universe_auto=false 后其余 auto_* 键自动失效，保留无害
        cfg = { ...cfg, universe: pool, universe_auto: false }
        message.info(
          `动态选股模板已固化：取原回测 ${segs.length} 段实际交易池并集 ${pool.length} 只作为寻优静态池`
        )
      }
      setTemplateConfig(cfg)
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
    // PARAM_FREEZE 约定：frozen 标记的参数不进入寻优空间（默认值即推荐值）
    const schemaItems = (s?.param_schema ?? []).filter((p) => !p.frozen)
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
    // 风控键仅放开核心（其余冻结：两轮寻优实证不敏感或已调定）
    const riskItems = RISK_FIELDS.filter((f) => f.key === 'max_holdings')
    return [
      ...bucket(schemaItems, '策略参数'),
      ...bucket(riskItems, '风控')
    ]
  }, [templateConfig, strategies])

  // 合法参数名全集：模板策略非 frozen 参数 + 核心风控键（导入 JSON 时校验用）
  const validKeys = useMemo(() => {
    if (!templateConfig) return new Set<string>()
    const s = strategies.find((x) => x.id === templateConfig.strategy_id)
    return new Set<string>([
      ...(s?.param_schema ?? []).filter((p) => !p.frozen).map((p) => p.key),
      'max_holdings'
    ])
  }, [templateConfig, strategies])

  const applyPreset = () => {
    const preset = PRESET_BY_STRATEGY[templateConfig?.strategy_id ?? '']
    if (!preset) {
      message.warning('该策略暂无内置预设，可使用「导入JSON」粘贴参数组')
      return
    }
    form.setFieldsValue({ groups: preset.groups })
    setMode('grouped')
    message.success(`已载入 ${preset.label}`)
  }

  const onImportJson = () => {
    try {
      const groups = parseGroupsJson(jsonText, validKeys)
      form.setFieldsValue({ groups })
      setMode('grouped')
      setJsonOpen(false)
      const total = groups.reduce((a, g) => a + g.n_trials, 0)
      message.success(`已导入 ${groups.length} 组（每轮共 ${total} trials）`)
    } catch (err) {
      message.error(`导入失败：${(err as Error).message}`)
    }
  }

  const onExportJson = () => {
    const vals = form.getFieldsValue(true) as OptimizeFormValues
    const text = groupsToJson(vals.groups)
    if (!text || text === '[]') {
      message.warning('当前参数组为空，先填好或导入后再导出')
      return
    }
    setExportText(text)
    setExportOpen(true)
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
        dd_floor: obj?.dd_floor,
        walk_forward_folds: obj?.walk_forward_folds ?? 3
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
        <Form.Item
          name={['objective', 'walk_forward_folds']}
          label="Walk-Forward 折数"
          initialValue={3}
          tooltip="样本内多折滚动评估（每折独立测试段），0/1=关闭（退化为单次70/30切分）"
        >
          <InputNumber min={0} max={6} style={{ width: '100%' }} />
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
                    disabled={!templateConfig || !PRESET_BY_STRATEGY[templateConfig.strategy_id]}
                    onClick={applyPreset}
                    title="一键填充当前策略的分层搜索空间预设（趋势/仓位/退出/做T等）"
                  >
                    载入策略预设
                  </Button>
                  <Button
                    icon={<ImportOutlined />}
                    disabled={!templateConfig}
                    onClick={() => {
                      setJsonText('')
                      setJsonOpen(true)
                    }}
                    title="粘贴 JSON 一次填入全部参数组"
                  >
                    导入JSON
                  </Button>
                  <Button
                    icon={<ExportOutlined />}
                    disabled={!templateConfig}
                    onClick={onExportJson}
                    title="把当前参数组导出为 JSON（可直接回贴到导入框）"
                  >
                    导出JSON
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

      <Modal
        open={jsonOpen}
        title="导入参数组 JSON"
        width={760}
        onCancel={() => setJsonOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setJsonOpen(false)}>取消</Button>,
          <Button key="import" type="primary" onClick={onImportJson}>解析并导入</Button>
        ]}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          支持三种写法：① 数组 <code>[{"{"}name, n_trials, params{"}"}]</code>；② 对象 <code>{"{"}组名: {"{"}n_trials, params{"}"}{"}"}</code>；
          ③ 单组对象（整体只写 params）。参数值：<code>[low, high]</code> / <code>[low, high, step]</code>
          （全整数=int，含小数=float）、字符串数组=select 候选、或对象 <code>{"{"}type, low, high, step{"}"}</code>。
          参数名必须是模板策略参数或风控字段，未知参数名将拒绝导入。
        </Typography.Paragraph>
        <Input.TextArea
          rows={14}
          value={jsonText}
          onChange={(e) => setJsonText(e.target.value)}
          placeholder={`[\n  {\n    "name": "选股排序",\n    "n_trials": 30,\n    "params": {\n      "mom_short": [5, 20, 5],\n      "w_accel": [0.1, 0.5, 0.1]\n    }\n  }\n]`}
        />
      </Modal>

      <Modal
        open={exportOpen}
        title="当前参数组 JSON（可直接回贴到导入框）"
        width={760}
        onCancel={() => setExportOpen(false)}
        footer={[
          <Button
            key="copy"
            type="primary"
            onClick={() => {
              navigator.clipboard.writeText(exportText)
                .then(() => message.success('已复制到剪贴板'))
                .catch(() => message.warning('复制失败，请手动全选复制'))
            }}
          >
            复制
          </Button>
        ]}
      >
        <Input.TextArea rows={16} value={exportText} readOnly style={{ fontFamily: 'monospace' }} />
      </Modal>
    </Space>
  )
}
