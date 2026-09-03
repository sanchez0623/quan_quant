import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Divider,
  Dropdown,
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
  Tag,
  Tooltip,
  Typography,
  Upload
} from 'antd'
import { DiffOutlined, ExportOutlined, ImportOutlined, PlayCircleOutlined, SaveOutlined } from '@ant-design/icons'
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
  ParamSchema,
  ParamValue,
  RiskConfig,
  Strategy,
  UniverseMeta
} from '../api/types'
import TaskStatusTag from '../components/TaskStatusTag'
import ParamSchemaForm from '../components/ParamSchemaForm'
import RiskConfigForm, { DEFAULT_RISK_CONFIG, RISK_FIELDS } from '../components/RiskConfigForm'
import BacktestRangePicker from '../components/BacktestRangePicker'
import StockPicker from '../components/StockPicker'

/** 模板配置 diff：把 params/risk_config/顶层标量拍平为可对比的 key -> value 映射 */
function flattenTemplateConfig(cfg: BacktestCreateRequest): Map<string, { group: string; value: unknown }> {
  const map = new Map<string, { group: string; value: unknown }>()
  const put = (group: string, key: string, value: unknown) => {
    if (value === undefined || value === null) return
    map.set(`${group}\u0000${key}`, { group, value })
  }
  Object.entries(cfg.params ?? {}).forEach(([k, v]) => put('策略参数', k, v))
  Object.entries(cfg.risk_config ?? {}).forEach(([k, v]) => put('风控', k, v))
  if (Array.isArray(cfg.universe)) put('基础', '股票池', cfg.universe.join(', '))
  const topKeys: Array<[string, string]> = [
    ['strategy_id', '策略'], ['period', '周期'], ['universe_auto', '动态选股'],
    ['start_date', '开始日期'], ['end_date', '结束日期'], ['initial_capital', '初始资金'],
    ['benchmark', '基准指数'], ['monthly_withdraw_base', '月提取额'],
    ['t_profit_withdraw_pct', 'T盈利提成'], ['min_t_amount', '最小T金额'],
    ['nav_take_profit_pct', '总资金止盈'], ['nav_take_profit_withdraw_pct', '止盈提取收益'],
    ['auto_idle_days', '空仓触发'], ['auto_top_x', '池子大小'], ['auto_above_ma', '均线锚'],
    ['auto_with_accel', '加速项'], ['auto_rank_key', '排序键'], ['exclude_st', '剔除ST'],
    ['pool_gate', '池级趋势开关'], ['pool_gate_enter_th', '趋势触发阈值']
  ]
  const rcfg = cfg as unknown as Record<string, unknown>
  topKeys.forEach(([k, label]) => {
    const v = rcfg[k]
    if (v !== undefined) put('基础', label, v)
  })
  return map
}

function fmtDiffVal(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') return String(Math.round(v * 100) / 100)
  if (Array.isArray(v)) return v.length ? v.join(', ') : '（空）'
  return String(v)
}

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
  nav_take_profit_pct?: number
  nav_take_profit_withdraw_pct?: number
  exclude_st?: boolean
  // ---- 动态选股（universe_auto）：分段滚动重选 ----
  universe_auto?: boolean
  auto_idle_days?: number
  auto_top_x?: number
  auto_above_ma?: number
  auto_with_accel?: boolean
  auto_min_rps?: number
  auto_index?: string[]
  auto_boards?: string[]
  auto_rank_key?: string
  benchmark?: string
  // ---- 池级趋势开关（POOL_GATE）----
  pool_gate?: boolean
  pool_gate_enter_th?: number
  params?: Record<string, string | number | boolean>
  risk_config?: Record<string, string | number>
  capital_preset?: string
}

/**
 * 表单值 -> 策略参数：以 param_schema 为准做「补全 + 剪枝」。
 *
 * 不直接把 antd store 的 params 原样搬进配置，原因有二：
 *  · 补全——advanced 收起、show_if 隐藏的参数可能从未挂载进表单，store 里没有值。
 *    参数改版新增参数时这类缺项会被静默丢掉（库里 momentum_t 老模板各缺 7 个
 *    做T重构新增参数，正是这么来的）；
 *  · 剪枝——antd 的 setFieldsValue 是深合并（要整体替换得用 setFields），
 *    切换策略后旧策略的参数键会残留在 params 里（模板 #15 momentum_slot 带着
 *    ma_cross 的 fast / slow / stop_loss_pct 已被实测到），而后端对
 *    params.stop_loss_pct 有「覆盖风控止损」的兼容分支，残留值会劫持止损。
 */
function pickSchemaParams(
  schema: ParamSchema[],
  formParams: unknown
): Record<string, ParamValue> {
  const src = (formParams ?? {}) as Record<string, unknown>
  const out: Record<string, ParamValue> = {}
  schema.forEach((p) => {
    const v = src[p.key]
    out[p.key] = (v === undefined || v === null || v === ''
      ? (p.default as ParamValue)
      : v) as ParamValue
  })
  return out
}

/** 表单值 -> 风控配置：以 RISK_FIELDS 为准，缺失用 DEFAULT_RISK_CONFIG 兜底；
 *  max_intraday_trades 无默认值，留空交给后端对齐策略 max_t_times。 */
function pickRiskConfig(formRisk: unknown): Record<string, string | number> {
  const src = (formRisk ?? {}) as Record<string, unknown>
  const out: Record<string, string | number> = {}
  RISK_FIELDS.forEach((f) => {
    let v = src[f.key]
    if (v !== undefined && v !== null && v !== '') {
      // 布尔型开关统一归一为数字 1/0：后端任务配置存 bool(true/false)（pydantic 强转），
      // 前端表单 select 用数字选项(1/0)。类型不一致会导致载入后下拉显示空白、
      // 规则说明判断错（floor===0 对 false 不成立），看起来像"没保存"。
      if (f.key === 'atr_trail_floor') {
        v = v === false || v === 0 || v === '0' ? 0 : 1
      }
      out[f.key] = v as string | number
    } else if (DEFAULT_RISK_CONFIG[f.key] !== undefined) {
      out[f.key] = DEFAULT_RISK_CONFIG[f.key]
    }
  })
  return out
}

/** 资金档预设：选择后一键填充 初始资金 / 最大持股 / 月提取 / 最小T金额 */
const CAPITAL_PRESETS: Record<string, { label: string; initial_capital: number; max_holdings: number; monthly_withdraw_base: number; min_t_amount: number }> = {
  '50w': { label: '50万档', initial_capital: 500000, max_holdings: 3, monthly_withdraw_base: 6000, min_t_amount: 30000 },
  '300w': { label: '300万档', initial_capital: 3000000, max_holdings: 5, monthly_withdraw_base: 20000, min_t_amount: 80000 }
}

/** 动态选股候选域选项（与后端 INDEX_REGISTRY / BOARD_LABELS 对齐） */
const AUTO_INDEX_OPTIONS = [
  { value: 'sz50', label: '上证50' },
  { value: 'hs300', label: '沪深300' },
  { value: 'zz500', label: '中证500' },
  { value: 'csi800', label: '中证800（=沪深300+中证500）' }
]
const AUTO_BOARD_OPTIONS = [
  { value: 'main', label: '主板' },
  { value: 'chinext', label: '创业板' },
  { value: 'star', label: '科创板' },
  { value: 'bse', label: '北交所' }
]

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
  // ---- 模板参数 diff ----
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffA, setDiffA] = useState<number | undefined>(undefined)
  const [diffB, setDiffB] = useState<number | undefined>(undefined)
  const prefillApplied = useRef(false)

  const strategy = useMemo(() => strategies.find((s) => s.id === strategyId), [strategies, strategyId])
  // 动态选股开关与开始日期联动（StockPicker 动量预筛需要 startDate，无后视镜）
  const universeAuto = Form.useWatch('universe_auto', form)
  const poolGate = Form.useWatch('pool_gate', form)
  const dateRangeWatch = Form.useWatch('dateRange', form)
  const startDate = dateRangeWatch?.[0] ? (dateRangeWatch[0] as Dayjs).format('YYYY-MM-DD') : undefined

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

  /**
   * 表单值 -> 回测配置（模板保存与提交共用）。
   * params / risk_config 一律以 schema / RISK_FIELDS 为准重建，保证落库配置
   * 「参数全量、无跨策略残留」，与后端 normalize_config 口径一致。
   */
  const buildConfigFromValues = useCallback((values: BacktestFormValues): BacktestCreateRequest => {
    const schema = strategies.find((s) => s.id === values.strategy_id)?.param_schema ?? []
    return {
      name: values.name ?? '',
      strategy_id: values.strategy_id,
      params: pickSchemaParams(schema, values.params),
      risk_config: pickRiskConfig(values.risk_config) as RiskConfig,
      // 动态选股：池子由后端动量预筛自动生成，universe 留空
      universe: values.universe_auto ? [] : (values.universe ?? []),
      universe_meta: universeMeta ?? null,
      universe_auto: values.universe_auto ?? false,
      auto_idle_days: values.auto_idle_days ?? 5,
      auto_top_x: values.auto_top_x ?? 30,
      auto_above_ma: values.auto_above_ma ?? 20,
      auto_with_accel: values.auto_with_accel ?? (values.strategy_id === 'momentum_slot'),
      auto_min_rps: values.auto_min_rps ?? null,
      auto_index: values.auto_index ?? [],
      auto_boards: values.auto_boards ?? [],
      auto_rank_key: values.auto_rank_key ?? 'score',
      benchmark: values.benchmark ?? '000905',
      pool_gate: values.pool_gate ?? false,
      pool_gate_enter_th: values.pool_gate_enter_th ?? 0.15,
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
      nav_take_profit_pct: values.nav_take_profit_pct ?? 0,
      nav_take_profit_withdraw_pct: values.nav_take_profit_withdraw_pct ?? 0,
      exclude_st: values.exclude_st ?? true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universeMeta, strategies])

  /** 回测配置 -> 表单（载入模板 / AI 建议预填共用） */
  const applyConfigToForm = useCallback(
    (cfg: BacktestCreateRequest, tip = '配置已载入') => {
      setStrategyId(cfg.strategy_id)
      setUniverseMeta(cfg.universe_meta ?? null)
      // 与默认值合并：老模板 / AI 建议可能缺新增字段（如自适应止损参数），
      // 缺项按 schema default 回填，避免表单出现空框、用户误以为未配置
      const schema = strategies.find((s) => s.id === cfg.strategy_id)?.param_schema ?? []
      const values: Record<string, unknown> = {
        name: cfg.name ?? '',
        strategy_id: cfg.strategy_id,
        period: cfg.period,
        universe: cfg.universe ?? [],
        initial_capital: cfg.initial_capital ?? 400000,
        exclude_st: cfg.exclude_st ?? true,
        universe_auto: cfg.universe_auto ?? false,
        auto_idle_days: cfg.auto_idle_days ?? 5,
        auto_top_x: cfg.auto_top_x ?? 30,
        auto_above_ma: cfg.auto_above_ma ?? 20,
        auto_with_accel: cfg.auto_with_accel ?? (cfg.strategy_id === 'momentum_slot'),
        ...(cfg.auto_min_rps != null ? { auto_min_rps: cfg.auto_min_rps } : {}),
        auto_index: cfg.auto_index ?? [],
        auto_boards: cfg.auto_boards ?? [],
        auto_rank_key: cfg.auto_rank_key ?? 'score',
        ...(cfg.benchmark != null ? { benchmark: cfg.benchmark } : {}),
        pool_gate: cfg.pool_gate ?? false,
        ...(cfg.pool_gate_enter_th != null ? { pool_gate_enter_th: cfg.pool_gate_enter_th } : {})
      }
      if (cfg.start_date && cfg.end_date) {
        values.dateRange = [dayjs(cfg.start_date), dayjs(cfg.end_date)]
      }
      const numericKeys = [
        'slippage_pct', 'commission_rate', 'commission_min', 'stamp_tax', 'transfer_fee',
        'handling_fee', 'regulatory_fee', 'warmup_days', 'monthly_withdraw_base',
        't_profit_withdraw_pct', 'min_t_amount', 'nav_take_profit_pct',
        'nav_take_profit_withdraw_pct'
      ] as const
      numericKeys.forEach((k) => {
        const v = cfg[k]
        if (v !== undefined && v !== null) values[k] = v
      })
      form.setFieldsValue(values as unknown as BacktestFormValues)
      // params / risk_config 必须整体替换：setFieldsValue 是深合并，上一个策略的
      // 参数键会留在 store 里（下一次保存就会混进新策略配置）
      form.setFields([
        { name: 'params', value: pickSchemaParams(schema, cfg.params) },
        { name: 'risk_config', value: pickRiskConfig(cfg.risk_config) }
      ])
      message.success(tip)
    },
    [form, strategies]
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
    // 用 setFields 整体替换 params（setFieldsValue 是深合并，旧策略的参数键会残留），
    // 否则下一次保存模板/提交回测时会把上一段策略的参数一起带进去
    form.setFields([
      { name: 'params', value: pickSchemaParams(s?.param_schema ?? [], {}) },
      { name: 'period', value: s?.periods?.[0] },
      // 加速项默认跟随策略：momentum_slot 开（预筛口径同其建仓）、momentum_t 关
      { name: 'auto_with_accel', value: id === 'momentum_slot' }
    ])
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

  /** 导出配置为 JSON 文件（schema 标记便于导入时校验） */
  const onExportConfig = (cfg: BacktestCreateRequest, name: string) => {
    const payload = { schema: 'quan_quant/backtest-config@1', name, config: cfg }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(name || 'backtest_config').replace(/[\\/:*?"<>|]/g, '_')}_${dayjs().format('YYYYMMDD')}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  /** 导入配置 JSON：识别 {schema,config} 包装或裸 config，回填表单 */
  const onImportConfig = (file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result))
        const cfg = parsed?.schema?.startsWith('quan_quant/') ? parsed.config : parsed
        if (!cfg || !cfg.strategy_id) {
          message.error('无效的配置文件：缺少 strategy_id')
          return
        }
        applyConfigToForm(cfg, '已导入配置，确认后可提交回测或存为模板')
      } catch {
        message.error('JSON 解析失败，请检查文件内容')
      }
    }
    reader.readAsText(file)
    return false // 阻止 Upload 自动上传
  }

  // ---- 模板参数 diff ----
  const paramLabelMap = useMemo(() => {
    const m: Record<string, string> = {}
    strategies.forEach((s) => s.param_schema?.forEach((p) => {
      if (p.label) m[p.key] = p.label
    }))
    return m
  }, [strategies])
  const riskLabelMap = useMemo(() => {
    const m: Record<string, string> = {}
    RISK_FIELDS.forEach((f) => {
      m[f.key] = f.label
    })
    return m
  }, [])
  const diffRows = useMemo(() => {
    const ca = templates.find((x) => x.id === diffA)?.config
    const cb = templates.find((x) => x.id === diffB)?.config
    if (!ca || !cb) return []
    const ma = flattenTemplateConfig(ca)
    const mb = flattenTemplateConfig(cb)
    const keys = new Set([...ma.keys(), ...mb.keys()])
    const order: Record<string, number> = { 策略参数: 0, 风控: 1, 基础: 2 }
    const rows: Array<{ group: string; label: string; a: string; b: string; diff: boolean }> = []
    keys.forEach((k) => {
      const ga = ma.get(k)?.group ?? mb.get(k)?.group ?? ''
      const rawKey = k.split('\u0000')[1]
      let label = rawKey
      if (ga === '策略参数') label = paramLabelMap[rawKey] ?? rawKey
      else if (ga === '风控') label = riskLabelMap[rawKey] ?? rawKey
      const va = ma.get(k)?.value
      const vb = mb.get(k)?.value
      const sa = fmtDiffVal(va)
      const sb = fmtDiffVal(vb)
      rows.push({ group: ga, label, a: sa, b: sb, diff: sa !== sb })
    })
    return rows.sort(
      (x, y) => (order[x.group] ?? 9) - (order[y.group] ?? 9) || x.label.localeCompare(y.label)
    )
  }, [templates, diffA, diffB, paramLabelMap, riskLabelMap])

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
            <Dropdown
              menu={{
                items: [
                  { key: 'export_form', label: '导出当前配置' },
                  { key: 'export_template', label: '导出选中模板', disabled: !selectedTemplateId }
                ],
                onClick: ({ key }) => {
                  if (key === 'export_form') {
                    const values = form.getFieldsValue(true)
                    onExportConfig(buildConfigFromValues(values), values.name || 'backtest_config')
                  } else if (key === 'export_template') {
                    const t = templates.find((x) => x.id === selectedTemplateId)
                    if (t) onExportConfig(t.config, t.name)
                  }
                }
              }}
            >
              <Button size="small" icon={<ExportOutlined />}>导出</Button>
            </Dropdown>
            <Upload accept=".json,application/json" showUploadList={false} beforeUpload={onImportConfig}>
              <Button size="small" icon={<ImportOutlined />}>导入</Button>
            </Upload>
            <Button
              size="small"
              icon={<DiffOutlined />}
              disabled={templates.length < 2}
              onClick={() => {
                setDiffA(templates[0]?.id)
                setDiffB(templates[1]?.id)
                setDiffOpen(true)
              }}
            >
              对比模板
            </Button>
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
            nav_take_profit_pct: 0,
            nav_take_profit_withdraw_pct: 0,
            exclude_st: true,
            universe_auto: false,
            auto_idle_days: 5,
            auto_top_x: 30,
            auto_above_ma: 20,
            auto_with_accel: false,
            auto_index: [],
            auto_boards: [],
            auto_rank_key: 'score',
            benchmark: '000905',
            pool_gate: false,
            pool_gate_enter_th: 0.15,
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
                label={universeAuto ? '股票池（动态选股：留空，自动生成）' : '股票池（手动选择 / 条件选股 / 动量趋势）'}
                rules={[{ required: !universeAuto, message: '请选择股票，或开启动态选股' }]}
              >
                <StockPicker
                  meta={universeMeta}
                  onMetaChange={(m) => setUniverseMeta(m ?? null)}
                  startDate={startDate}
                  disabled={universeAuto}
                />
              </Form.Item>
              <Form.Item name="universe_auto" valuePropName="checked" style={{ marginBottom: 8 }}>
                <Checkbox>
                  动态选股（滚动重选）：全空仓 N 个交易日后自动重跑动量趋势预筛换池
                  {strategyId && !['momentum_t', 'momentum_slot'].includes(strategyId)
                    ? '（仅支持 momentum_t / momentum_slot）' : ''}
                </Checkbox>
              </Form.Item>
              {universeAuto && (
                <Space size="large" wrap style={{ marginBottom: 8 }}>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>空仓触发（交易日）：</Typography.Text>
                    <Form.Item name="auto_idle_days" noStyle>
                      <InputNumber size="small" min={1} max={60} />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>池子大小：</Typography.Text>
                    <Form.Item name="auto_top_x" noStyle>
                      <InputNumber size="small" min={1} max={500} />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>均线锚：</Typography.Text>
                    <Form.Item name="auto_above_ma" noStyle>
                      <Select
                        size="small" style={{ width: 118 }}
                        options={[
                          { value: 20, label: 'MA20（slot）' },
                          { value: 60, label: 'MA60（t）' },
                          { value: 120, label: 'MA120' }
                        ]}
                      />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>加速项：</Typography.Text>
                    <Form.Item name="auto_with_accel" noStyle valuePropName="checked">
                      <Switch size="small" />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>RPS≥：</Typography.Text>
                    <Form.Item name="auto_min_rps" noStyle>
                      <InputNumber size="small" min={0} max={100} placeholder="不限" style={{ width: 70 }} />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>排序键：</Typography.Text>
                    <Form.Item name="auto_rank_key" noStyle>
                      <Select
                        size="small" style={{ width: 108 }}
                        options={[
                          { value: 'score', label: '累计强度' },
                          { value: 'accel', label: '加速度' },
                          { value: 'fresh', label: '金叉新鲜' },
                          { value: 'mom_gap', label: '短中差值' }
                        ]}
                      />
                    </Form.Item>
                  </Space>
                </Space>
              )}
              {universeAuto && (
                <Space size="large" wrap style={{ marginBottom: 8 }}>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>指数域：</Typography.Text>
                    <Form.Item name="auto_index" noStyle>
                      <Select
                        mode="multiple"
                        maxTagCount="responsive"
                        allowClear
                        placeholder="全市场"
                        options={AUTO_INDEX_OPTIONS}
                        style={{ width: 260 }}
                        size="small"
                      />
                    </Form.Item>
                  </Space>
                  <Space size={4}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>板块域：</Typography.Text>
                    <Form.Item name="auto_boards" noStyle>
                      <Checkbox.Group options={AUTO_BOARD_OPTIONS} />
                    </Form.Item>
                  </Space>
                </Space>
              )}
              {universeAuto && (
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                  每段池子以「段首前一交易日」收盘的动量预筛生成（无后视镜）；全市场无票过门槛时保持空仓，绝不硬买。
                </Typography.Text>
              )}
              <Form.Item
                name="pool_gate"
                valuePropName="checked"
                style={{ marginBottom: 8 }}
                tooltip="池级趋势开关（POOL_GATE）：池内动量分为正的票占比连续 2 日低于触发阈值时，抑制开新仓/加仓；持仓退出与做T照常。健康度回升至 2×阈值连续 2 日后自动恢复。"
              >
                <Checkbox>
                  池级趋势开关：环境不适配时自动停开新仓（治「下跌市反复开仓反复止损」的环境税）
                  {strategyId && !['momentum_t', 'momentum_slot'].includes(strategyId)
                    ? '（仅支持 momentum_t / momentum_slot）' : ''}
                </Checkbox>
              </Form.Item>
              {poolGate && (
                <Space size={4} style={{ marginBottom: 8 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>触发阈值（健康度）：</Typography.Text>
                  <Form.Item name="pool_gate_enter_th" noStyle>
                    <InputNumber size="small" min={0.02} max={0.49} step={0.05} style={{ width: 80 }} />
                  </Form.Item>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    （恢复线 = 2×触发值，内置）
                  </Typography.Text>
                </Space>
              )}
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
                name="benchmark"
                label="基准指数"
                extra="报告净值图叠加基准对比 + 超额收益指标"
              >
                <Select
                  allowClear
                  placeholder="默认中证500"
                  options={[
                    { value: '000905', label: '中证500' },
                    { value: '000300', label: '沪深300' }
                  ]}
                />
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
                label: '账户与月度出金（逐笔T盈利提成 + 月末兜底 + 总资金止盈提取；0 关闭）',
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
                        name="nav_take_profit_pct"
                        label="总资金止盈（%）"
                        extra="净值相对上次提取后基准涨幅达阈值即触发；0=关闭"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} max={1000} step={5} />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        name="nav_take_profit_withdraw_pct"
                        label="止盈提取收益（%）"
                        extra="触发时按比例提取收益部分（本金不动）；0=关闭"
                      >
                        <InputNumber style={{ width: '100%' }} min={0} max={100} step={5} />
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

      <Modal
        title="模板参数对比"
        open={diffOpen}
        onCancel={() => setDiffOpen(false)}
        footer={null}
        width={960}
        destroyOnClose
      >
        <Space size="large" style={{ marginBottom: 12 }}>
          <Space size={4}>
            <Typography.Text type="secondary">模板A：</Typography.Text>
            <Select
              style={{ width: 220 }}
              value={diffA}
              onChange={setDiffA}
              showSearch
              optionFilterProp="label"
              options={templates.map((t) => ({ value: t.id, label: t.name }))}
            />
          </Space>
          <Space size={4}>
            <Typography.Text type="secondary">模板B：</Typography.Text>
            <Select
              style={{ width: 220 }}
              value={diffB}
              onChange={setDiffB}
              showSearch
              optionFilterProp="label"
              options={templates.map((t) => ({ value: t.id, label: t.name }))}
            />
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            红色 = 两模板不同
          </Typography.Text>
        </Space>
        <Table
          rowKey={(r) => `${r.group}-${r.label}`}
          size="small"
          dataSource={diffRows}
          pagination={false}
          columns={[
            {
              title: '分组',
              dataIndex: 'group',
              width: 90,
              render: (v: string) => <Tag>{v}</Tag>
            },
            { title: '参数', dataIndex: 'label' },
            {
              title: '模板A',
              dataIndex: 'a',
              render: (v: string, r) => (
                <span style={r.diff ? { color: '#cf1322', fontWeight: 500 } : undefined}>{v}</span>
              )
            },
            {
              title: '模板B',
              dataIndex: 'b',
              render: (v: string, r) => (
                <span style={r.diff ? { color: '#cf1322', fontWeight: 500 } : undefined}>{v}</span>
              )
            },
            {
              title: '',
              dataIndex: 'diff',
              width: 64,
              render: (d: boolean) => (d ? <Tag color="red">不同</Tag> : null)
            }
          ]}
        />
      </Modal>
    </Space>
  )
}
