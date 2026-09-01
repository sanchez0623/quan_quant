import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Input,
  InputNumber,
  message,
  Popover,
  Radio,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  TreeSelect,
  Typography
} from 'antd'
import { AppstoreAddOutlined, LockOutlined, ReloadOutlined } from '@ant-design/icons'
import { errDetail, getPickOptions, getStocks, getStocksByCodes, pickStocks } from '../api/client'
import type {
  PickIndustryNode,
  PickOptions,
  PickResponse,
  StockItem,
  UniverseMeta
} from '../api/types'

type PickMode = 'manual' | 'condition' | 'momentum'

interface StockPickerProps {
  /** 受控：当前股票池代码（Form.Item 自动注入 value） */
  value?: string[]
  onChange?: (codes: string[]) => void
  /** 条件选股溯源 meta（模板载入后传入以锁定 seed） */
  meta?: UniverseMeta | null
  onMetaChange?: (meta?: UniverseMeta | null) => void
  disabled?: boolean
  /** 动量趋势预筛的基准日来源（=回测开始日，取其前一交易日收盘数据）；
   * 为空时动量模式不可用——防止用未来数据选股（无后视镜） */
  startDate?: string
}

/** 预览/已应用股票代码+名称 Tag 流（最多显示 N 个，可展开全部） */
function CodeTags({ nameMap, codes }: { nameMap: Record<string, string>; codes: string[] }) {
  const [expanded, setExpanded] = useState(false)
  const showAll = expanded || codes.length <= 20
  const shown = showAll ? codes : codes.slice(0, 20)
  if (codes.length === 0) return <Typography.Text type="secondary">（空）</Typography.Text>
  return (
    <Space size={[4, 4]} wrap>
      {shown.map((c) => (
        <Tag key={c} style={{ marginInlineEnd: 0 }}>
          {c} {nameMap[c] ? ` ${nameMap[c]}` : ''}
        </Tag>
      ))}
      {!showAll && (
        <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setExpanded(true)}>
          展开全部（共 {codes.length} 只）
        </Button>
      )}
    </Space>
  )
}

export default function StockPicker({
  value,
  onChange,
  meta,
  onMetaChange,
  disabled,
  startDate
}: StockPickerProps) {
  const codes = value ?? []
  const [mode, setMode] = useState<PickMode>('manual')
  // ---- 手动模式：远程搜索 + 批量粘贴 ----
  const [stocks, setStocks] = useState<StockItem[]>([])
  const [stockSearching, setStockSearching] = useState(false)
  const [batchOpen, setBatchOpen] = useState(false)
  const [batchText, setBatchText] = useState('')
  const [batchLoading, setBatchLoading] = useState(false)
  const searchTimer = useRef<number | null>(null)
  // ---- 条件选股：选项 / 筛选 / 抽样 / 预览 ----
  const [options, setOptions] = useState<PickOptions | null>(null)
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [index, setIndex] = useState<string[]>([])
  const [industryKeys, setIndustryKeys] = useState<string[]>([])
  const [boards, setBoards] = useState<string[]>([])
  const [excludeSt, setExcludeSt] = useState(true)
  const [n, setN] = useState<number | undefined>(20)
  const [seed, setSeed] = useState<number | undefined>(42)
  const [lockSeed, setLockSeed] = useState(false)
  const [preview, setPreview] = useState<PickResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  // ---- 动量趋势预筛：参数（与动态选股 universe_auto 同一套口径） ----
  const [moTopX, setMoTopX] = useState<number>(30)
  const [moAboveMa, setMoAboveMa] = useState<number>(60)
  const [moAccel, setMoAccel] = useState(false)
  const [moMinRps, setMoMinRps] = useState<number | undefined>(undefined)
  // 已应用池子的名称映射（供 Tag 展示；外部载入时按需回填）
  const [nameMap, setNameMap] = useState<Record<string, string>>({})

  // 行业三级树 value 集：勾选任一节点时把三级叶子 value 收集起来传给后端 l1/l2/l3
  const leafValues = useMemo(() => {
    const out: string[] = []
    const walk = (nodes: PickIndustryNode[] | undefined) => {
      nodes?.forEach((node) => {
        if (node.children?.length) walk(node.children)
        else out.push(node.value)
      })
    }
    walk(options?.industry_tree)
    return out
  }, [options])

  const industryFilters = useMemo(() => {
    const sel = new Set(industryKeys)
    const l1 = new Set<string>()
    const l2 = new Set<string>()
    const l3 = new Set<string>()
    const walk = (nodes: PickIndustryNode[] | undefined, l1Name?: string, l2Name?: string) => {
      nodes?.forEach((node) => {
        if (sel.has(node.value)) {
          if (l1Name === undefined) l1.add(node.value)
          else if (l2Name === undefined) l2.add(node.value)
          else l3.add(node.value)
        }
        walk(node.children, l1Name ?? node.value, l2Name ?? node.value)
      })
    }
    walk(options?.industry_tree)
    return { industry_l1: [...l1], industry_l2: [...l2], industry_l3: [...l3] }
  }, [industryKeys, options])

  // 数据未就绪标记：行业或指数任一缺失 -> 顶部 Alert 引导去数据管理页
  const dataMissing = !!options && (!options.industry_snapshot || !options.index_snapshot)

  // ---- 载入 pick options ----
  useEffect(() => {
    setOptionsLoading(true)
    getPickOptions()
      .then(setOptions)
      .catch(() => setOptions(null))
      .finally(() => setOptionsLoading(false))
  }, [])

  // 模板载入带 meta -> 锁定 seed 并回填 seed（防手滑重新抽样破坏复现）
  useEffect(() => {
    if (meta?.seed_used) {
      setSeed(meta.seed_used)
      setLockSeed(true)
    }
  }, [meta])

  // 外部设置 universe（模板载入/手动添加）时按需回填名称
  useEffect(() => {
    if (!codes.length) return
    const missing = codes.filter((c) => !nameMap[c])
    if (!missing.length) return
    getStocksByCodes(missing)
      .then((items) =>
        setNameMap((prev) => {
          const next = { ...prev }
          items.forEach((s) => (next[s.code] = s.name))
          return next
        })
      )
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codes])

  // ---- 手动模式：远程搜索（防抖 300ms） ----
  const onStockSearch = (kw: string) => {
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
    if (!kw) {
      setStocks([])
      return
    }
    searchTimer.current = window.setTimeout(async () => {
      setStockSearching(true)
      try {
        setStocks(await getStocks(kw, 100))
      } catch {
        /* ignore */
      } finally {
        setStockSearching(false)
      }
    }, 300)
  }

  // ---- 手动模式：批量粘贴 ----
  const addBatchCodes = async () => {
    const parsed = [...new Set(batchText.match(/\d{6}/g) ?? [])]
    if (parsed.length === 0) {
      message.warning('未识别到有效的 6 位股票代码')
      return
    }
    setBatchLoading(true)
    try {
      const items = await getStocksByCodes(parsed)
      if (items.length === 0) {
        message.warning('未匹配到任何股票，请确认代码是否正确')
        return
      }
      const merged = [...new Set([...codes, ...items.map((s) => s.code)])]
      onChange?.(merged)
      setNameMap((prev) => {
        const next = { ...prev }
        items.forEach((s) => (next[s.code] = s.name))
        return next
      })
      const missing = parsed.filter((c) => !items.some((s) => s.code === c))
      if (missing.length > 0) {
        message.warning(`已添加 ${items.length} 只；以下代码未匹配: ${missing.join(', ')}`)
      } else {
        message.success(`已添加 ${items.length} 只股票`)
      }
      setBatchText('')
      setBatchOpen(false)
    } catch (err) {
      message.error(errDetail(err, '批量添加失败'))
    } finally {
      setBatchLoading(false)
    }
  }

  // ---- 条件选股 / 动量预筛：预览 ----
  const doPreview = useCallback(async () => {
    if (mode === 'manual') return
    if (mode === 'momentum' && !startDate) return
    setPreviewLoading(true)
    try {
      if (mode === 'momentum') {
        // 动量趋势预筛：基准日 = 回测开始日前一交易日（无后视镜，后端实现）
        const res = await pickStocks({
          filters: {
            ...industryFilters,
            index,
            boards,
            exclude_st: excludeSt,
            momentum: { top_x: moTopX, above_ma: moAboveMa, with_accel: moAccel, min_rps: moMinRps ?? null }
          },
          random: null,
          as_of: startDate ?? null
        })
        setPreview(res)
      } else {
        const random = { n: n ?? undefined, seed: lockSeed ? seed : undefined }
        const res = await pickStocks({ filters: { ...industryFilters, index, boards, exclude_st: excludeSt }, random })
        setPreview(res)
      }
    } catch (err) {
      message.error(errDetail(err, '预览失败'))
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, industryFilters, index, boards, excludeSt, n, seed, lockSeed,
      startDate, moTopX, moAboveMa, moAccel, moMinRps])

  // 仅条件选股自动预览（动量预筛需全市场特征计算，手动触发）
  useEffect(() => {
    if (mode !== 'condition') return
    const timer = window.setTimeout(doPreview, 300)
    return () => window.clearTimeout(timer)
  }, [doPreview, mode])

  // 重新抽样：仅随机化 seed 后重调（过滤条件不变）
  const onResample = () => {
    const newSeed = Math.floor(Math.random() * 2 ** 31)
    setSeed(newSeed)
    message.info(`已更换随机种子：${newSeed}`)
  }

  // 应用为股票池：唯一写入口
  const onApply = () => {
    if (!preview || preview.codes.length === 0) {
      message.warning('预览为空，请先调整筛选条件')
      return
    }
    onChange?.(preview.codes)
    onMetaChange?.(preview.meta)
    setNameMap(preview.name_map)
    message.success(`已应用 ${preview.codes.length} 只股票为股票池`)
  }

  const treeData = options?.industry_tree

  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Radio.Group
        value={mode}
        onChange={(e) => setMode(e.target.value as PickMode)}
        optionType="button"
        size="small"
        disabled={disabled}
        options={[
          { value: 'manual', label: '手动选择' },
          { value: 'condition', label: '条件选股' },
          { value: 'momentum', label: '动量趋势' }
        ]}
      />

      {mode === 'manual' ? (
        <>
          <Select
            mode="multiple"
            placeholder="输入代码或名称搜索（可批量粘贴）"
            filterOption={false}
            onSearch={onStockSearch}
            notFoundContent={stockSearching ? <Spin size="small" /> : null}
            options={stocks.map((s) => ({
              value: s.code,
              label: `${s.code} ${s.name}${s.st ? ' (ST)' : ''}`
            }))}
            value={codes}
            onChange={(v) => onChange?.(v)}
            allowClear
            disabled={disabled}
            style={{ width: '100%' }}
          />
          <Popover
            open={batchOpen}
            onOpenChange={setBatchOpen}
            trigger="click"
            content={
              <div style={{ width: 320 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  粘贴多个 6 位代码，逗号/空格/换行分隔（支持 sh.600000 前缀），自动解析名称并合并进股票池
                </Typography.Text>
                <Input.TextArea
                  value={batchText}
                  onChange={(e) => setBatchText(e.target.value)}
                  rows={4}
                  placeholder={'600000, 000001\n600036 601318'}
                  style={{ margin: '8px 0' }}
                />
                <Button type="primary" size="small" block loading={batchLoading} onClick={addBatchCodes}>
                  添加到股票池
                </Button>
              </div>
            }
          >
            <Button type="link" size="small" style={{ padding: 0 }} disabled={disabled}>
              批量添加
            </Button>
          </Popover>
        </>
      ) : mode === 'momentum' ? (
        <>
          {!startDate ? (
            <Alert
              type="warning"
              showIcon
              message="需先确认回测时间范围"
              description="动量趋势预筛基于「回测开始日前一交易日」的收盘数据选股（无后视镜），请先在表单中填写回测开始日期。"
            />
          ) : (
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  限定候选域（可选，缺省=全市场剔ST/退市）——指数成分（多选=并集）：
                </Typography.Text>
                <Select
                  placeholder="全市场"
                  allowClear
                  mode="multiple"
                  maxTagCount="responsive"
                  value={index}
                  onChange={setIndex}
                  options={(options?.indices ?? []).map((i) => ({
                    value: i.key,
                    label: `${i.name}（${i.count}）`
                  }))}
                  style={{ width: '100%' }}
                  size="small"
                  disabled={disabled}
                />
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  板块（多选=并集；与指数域取交集）：
                </Typography.Text>
                <Checkbox.Group
                  value={boards}
                  onChange={(v) => setBoards(v as string[])}
                  options={(options?.boards ?? []).map((b) => ({
                    value: b.key,
                    label: `${b.name}（${b.count}）`
                  }))}
                  disabled={disabled}
                />
              </div>
              <Space size="large" wrap>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>取前 x 只：</Typography.Text>
                  <InputNumber
                    size="small" min={1} max={500} value={moTopX}
                    onChange={(v) => setMoTopX(v ?? 30)} style={{ width: 80 }} disabled={disabled}
                  />
                </Space>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>站上均线：</Typography.Text>
                  <Select
                    size="small" value={moAboveMa} onChange={setMoAboveMa} style={{ width: 92 }}
                    options={[
                      { value: 20, label: 'MA20（slot）' },
                      { value: 60, label: 'MA60（t）' },
                      { value: 120, label: 'MA120' }
                    ]}
                    disabled={disabled}
                  />
                </Space>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>加速项：</Typography.Text>
                  <Switch size="small" checked={moAccel} onChange={setMoAccel} disabled={disabled} />
                </Space>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>RPS≥：</Typography.Text>
                  <InputNumber
                    size="small" min={0} max={100} value={moMinRps}
                    onChange={(v) => setMoMinRps(v ?? undefined)} placeholder="不限"
                    style={{ width: 70 }} disabled={disabled}
                  />
                </Space>
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                门槛内置：MACD金叉 + 站上均线 + 动量分为正 + 崩溃保护（防追高）；
                基准日 = {startDate} 前一交易日（与策略 T-1 建仓口径一致）。
              </Typography.Text>
              <Divider style={{ margin: '4px 0' }} />
              <Space align="start">
                <Button type="primary" size="small" loading={previewLoading} onClick={doPreview} disabled={disabled}>
                  预览
                </Button>
                <Button
                  size="small"
                  icon={<AppstoreAddOutlined />}
                  onClick={onApply}
                  disabled={disabled || !preview || preview.codes.length === 0}
                >
                  应用为股票池
                </Button>
                {preview && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    基准日 {preview.meta?.snapshot_date ?? '-'}：符合门槛 {preview.total_matched} 只 → 取前 {preview.total_picked} 只
                  </Typography.Text>
                )}
              </Space>
              {preview && preview.items && preview.items.length > 0 && (
                <Table
                  size="small"
                  rowKey="code"
                  dataSource={preview.items}
                  pagination={{ pageSize: 8, size: 'small' }}
                  columns={[
                    { title: '#', dataIndex: 'rank', width: 44 },
                    { title: '代码', dataIndex: 'code', width: 80 },
                    { title: '名称', dataIndex: 'name', width: 96, ellipsis: true },
                    {
                      title: '动量分', dataIndex: 'score', width: 84,
                      render: (v: number) => (typeof v === 'number' ? v.toFixed(3) : '-')
                    },
                    {
                      title: 'RPS', dataIndex: 'rps', width: 64,
                      render: (v: number | null) => (v != null ? v.toFixed(1) : '-')
                    }
                  ]}
                />
              )}
            </Space>
          )}
        </>
      ) : (
        <>
          {dataMissing && (
            <Alert
              type="warning"
              showIcon
              message="行业/成分数据未更新"
              description="请先到「数据管理」页点击「更新行业与成分」，完成后即可按行业/指数筛选。"
            />
          )}
          {optionsLoading && !options ? (
            <Spin size="small" />
          ) : (
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  指数成分（可多选，并集）：
                </Typography.Text>
                <Select
                  placeholder="不限指数"
                  allowClear
                  mode="multiple"
                  maxTagCount="responsive"
                  value={index}
                  onChange={setIndex}
                  options={(options?.indices ?? []).map((i) => ({
                    value: i.key,
                    label: `${i.name}（${i.count}）`
                  }))}
                  style={{ width: '100%' }}
                  size="small"
                  disabled={disabled}
                />
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  板块（可多选，并集）：
                </Typography.Text>
                <Checkbox.Group
                  value={boards}
                  onChange={(v) => setBoards(v as string[])}
                  options={(options?.boards ?? []).map((b) => ({
                    value: b.key,
                    label: `${b.name}（${b.count}）`
                  }))}
                  disabled={disabled}
                />
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  申万行业（可多选，勾选父级=全选子级）：
                </Typography.Text>
                <TreeSelect
                  treeData={treeData}
                  value={industryKeys}
                  onChange={setIndustryKeys}
                  multiple
                  treeCheckable
                  showCheckedStrategy={TreeSelect.SHOW_ALL}
                  placeholder="不限行业"
                  allowClear
                  treeDefaultExpandAll={false}
                  maxTagCount="responsive"
                  style={{ width: '100%' }}
                  size="small"
                  disabled={disabled}
                />
              </div>
              <Space size="large">
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>剔除ST：</Typography.Text>
                  <Switch size="small" checked={excludeSt} onChange={setExcludeSt} disabled={disabled} />
                </Space>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>数量 n：</Typography.Text>
                  <InputNumber
                    size="small"
                    min={1}
                    value={n}
                    onChange={(v) => setN(v ?? undefined)}
                    placeholder="全量"
                    disabled={disabled}
                  />
                </Space>
                <Space size={4}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    种子 seed：{lockSeed && <LockOutlined style={{ color: '#faad14' }} />}
                  </Typography.Text>
                  <InputNumber
                    size="small"
                    min={0}
                    value={seed}
                    onChange={(v) => setSeed(v ?? undefined)}
                    disabled={disabled || lockSeed}
                    style={{ width: 110 }}
                  />
                  <Button size="small" icon={<ReloadOutlined />} onClick={onResample} disabled={disabled || lockSeed}>
                    重新抽样
                  </Button>
                  <Switch
                    size="small"
                    checked={lockSeed}
                    onChange={setLockSeed}
                    checkedChildren="锁定"
                    unCheckedChildren="解锁"
                    disabled={disabled}
                  />
                </Space>
              </Space>
              {lockSeed && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  种子已锁定（来自已应用的选股溯源 meta），确保模板载入/实验复现得到相同池子。
                </Typography.Text>
              )}
              <Divider style={{ margin: '4px 0' }} />
              <Space align="start">
                <Button type="primary" size="small" loading={previewLoading} onClick={doPreview}>
                  预览
                </Button>
                <Button
                  size="small"
                  icon={<AppstoreAddOutlined />}
                  onClick={onApply}
                  disabled={disabled || !preview || preview.codes.length === 0}
                >
                  应用为股票池
                </Button>
                {preview && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    命中 {preview.total_matched} 只 → 抽取 {preview.total_picked} 只
                    {preview.seed_used != null ? `（seed=${preview.seed_used}）` : ''}
                    {preview.truncated ? '（命中数不足，已全取）' : ''}
                  </Typography.Text>
                )}
              </Space>
              {preview && preview.codes.length > 0 && (
                <div>
                  <CodeTags nameMap={preview.name_map} codes={preview.codes} />
                </div>
              )}
            </Space>
          )}
        </>
      )}

      {/* 已应用股票池（两种模式共用） */}
      {codes.length > 0 && mode === 'manual' && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          已选 {codes.length} 只
        </Typography.Text>
      )}
      {codes.length > 0 && (mode === 'condition' || mode === 'momentum') && (
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已应用股票池（{codes.length} 只）
            {mode === 'momentum' && meta?.snapshot_date
              ? `，基准日 ${meta.snapshot_date}`
              : meta?.seed_used != null
                ? `，seed=${meta.seed_used}，命中 ${meta.total_matched ?? '-'}`
                : ''}
            ：
          </Typography.Text>
          <div style={{ marginTop: 4 }}>
            <CodeTags nameMap={nameMap} codes={codes} />
          </div>
        </div>
      )}
    </Space>
  )
}
