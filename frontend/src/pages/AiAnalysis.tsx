import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  message,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography
} from 'antd'
import { PlayCircleOutlined, ThunderboltOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import { useNavigate } from 'react-router-dom'
import {
  clearAiUsage,
  createBacktest,
  errDetail,
  getAiAnalyses,
  getAiProfiles,
  getAiSuggestionStats,
  getBacktestReport,
  getBacktests,
  startAiAnalyze
} from '../api/client'
import type {
  AiAnalysisItem,
  AiFinding,
  AiProfilesResponse,
  AiSuggestions,
  AiSuggestionStats,
  AiValidation,
  BacktestCreateRequest,
  BacktestListItem
} from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import { fmtInt } from '../utils/format'

/** 风控字段中文标签（建议卡片展示用） */
const RISK_LABELS: Record<string, string> = {
  max_position_pct_per_stock: '单票最大仓位(%)',
  max_total_position_pct: '总仓位上限(%)',
  stop_loss_mode: '止损模式',
  stop_loss_pct: '固定止损(%)',
  atr_period: 'ATR周期',
  atr_multiplier: 'ATR倍数',
  take_profit_pct: '止盈(%)',
  trailing_stop_pct: '移动止损(%)',
  atr_trail_mult: '移动锁盈倍数k2',
  atr_cost_base: '成本基准',
  atr_trail_floor: '止损线棘轮',
  adaptive: '自适应止损',
  adaptive_trend_ma: '趋势均线周期',
  adaptive_slope_n: '斜率窗口',
  adaptive_k_loose: '趋势市放宽倍数',
  adaptive_k_tight: '破位收紧倍数',
  adaptive_vol_n: '波动分位窗口',
  adaptive_vol_hi: '高波分位',
  adaptive_vol_lo: '低波分位',
  max_drawdown_breaker: '回撤熔断(%)',
  max_intraday_trades: '日内最大交易次数',
  max_holdings: '最大持仓只数',
  cash_reserve_pct: '现金缓冲(%)'
}

/** 验证 A/B 对比指标中文标签 */
const METRIC_LABELS: Record<string, string> = {
  total_return: '总收益',
  sharpe: '夏普比率',
  calmar: '卡玛比率',
  win_rate: '胜率',
  profit_loss_ratio: '盈亏比',
  max_drawdown: '最大回撤'
}

/** 百分比口径指标（展示为 %） */
const PCT_METRICS = new Set(['total_return', 'max_drawdown', 'win_rate'])

function fmtMetric(key: string, v: number): string {
  if (PCT_METRICS.has(key)) return `${(v * 100).toFixed(1)}%`
  return v.toFixed(2)
}

const SEVERITY_COLORS: Record<AiFinding['severity'], string> = {
  high: 'red',
  medium: 'orange',
  low: 'blue'
}

const VERDICT_COLORS: Record<string, string> = {
  改善: 'green',
  持平: 'default',
  恶化: 'red'
}

export default function AiAnalysis() {
  const navigate = useNavigate()
  const [backtests, setBacktests] = useState<BacktestListItem[]>([])
  const [profilesResp, setProfilesResp] = useState<AiProfilesResponse | null>(null)
  const [backtestId, setBacktestId] = useState<string | undefined>(undefined)
  const [profile, setProfile] = useState<string | undefined>(undefined)
  const [analyses, setAnalyses] = useState<AiAnalysisItem[]>([])
  const [selectedAnalysisId, setSelectedAnalysisId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null)
  const [loadingAnalyses, setLoadingAnalyses] = useState(false)
  const [runningDirect, setRunningDirect] = useState(false)
  const [baseConfig, setBaseConfig] = useState<BacktestCreateRequest | null>(null)
  const [stats, setStats] = useState<AiSuggestionStats | null>(null)

  const loadStats = useCallback(async () => {
    try {
      setStats(await getAiSuggestionStats())
    } catch {
      /* ignore */
    }
  }, [])

  const loadBacktests = useCallback(async () => {
    try {
      setBacktests(await getBacktests())
    } catch {
      /* ignore */
    }
  }, [])

  const loadProfiles = useCallback(async () => {
    try {
      setProfilesResp(await getAiProfiles())
    } catch {
      /* ignore */
    }
  }, [])

  useEffect(() => {
    loadBacktests()
    loadProfiles()
  }, [loadBacktests, loadProfiles])

  const successBacktests = useMemo(
    () => backtests.filter((b) => b.status === 'success'),
    [backtests]
  )
  const userKeyPool = useMemo(() => profilesResp?.user_key_pool ?? [], [profilesResp])
  const hasEnvPool = useMemo(
    () => (profilesResp?.key_pool?.length ?? 0) > 0,
    [profilesResp]
  )
  const availableProfiles = useMemo(
    () => (profilesResp?.profiles ?? []).filter((p) => p.available),
    [profilesResp]
  )
  // 可用 = 用户自己的 Key 池 / 系统环境变量池 / 旧 profiles 任一存在
  const hasAnyKey = userKeyPool.length > 0 || hasEnvPool || availableProfiles.length > 0

  useEffect(() => {
    if (!profile) {
      setProfile('auto')
    }
  }, [profile])

  const loadAnalyses = useCallback(async (bid: string) => {
    setLoadingAnalyses(true)
    try {
      const list = await getAiAnalyses(bid)
      setAnalyses(list)
      setSelectedAnalysisId((prev) =>
        prev && list.some((a) => a.task_id === prev) ? prev : (list[0]?.task_id ?? null)
      )
    } catch {
      /* ignore */
    } finally {
      setLoadingAnalyses(false)
    }
  }, [])

  useEffect(() => {
    if (backtestId) {
      loadAnalyses(backtestId)
      // 拉取该回测的原始配置（建议卡片展示旧值 + 应用建议时作为合并基底）
      getBacktestReport(backtestId)
        .then((r) => setBaseConfig(r.config))
        .catch(() => setBaseConfig(null))
    } else {
      setAnalyses([])
      setSelectedAnalysisId(null)
      setBaseConfig(null)
    }
  }, [backtestId, loadAnalyses])

  const { progress, message: taskMessage } = useTaskProgress(currentTaskId, (s) => {
    if (s === 'success') {
      message.success('AI 分析完成')
    } else {
      message.error('AI 分析失败')
    }
    setCurrentTaskId(null)
    if (backtestId) loadAnalyses(backtestId)
    loadProfiles()
    loadStats()
  })

  const startAnalyze = async () => {
    if (!backtestId) {
      message.warning('请选择回测任务')
      return
    }
    if (!profile) {
      message.warning('请选择使用的 Key')
      return
    }
    setStarting(true)
    try {
      const res = await startAiAnalyze({
        backtest_id: backtestId,
        profile: profile === 'auto' ? undefined : profile
      })
      setCurrentTaskId(res.task_id)
      message.info('分析任务已提交')
    } catch (err) {
      message.error(errDetail(err, '提交分析失败'))
    } finally {
      setStarting(false)
    }
  }

  const selected = analyses.find((a) => a.task_id === selectedAnalysisId) ?? null
  const usage = profilesResp?.usage
  const findings: AiFinding[] = selected?.diagnostics ?? []
  const validation: AiValidation | null = selected?.validation ?? null

  const suggestions = useMemo<AiSuggestions | null>(() => {
    const s = selected?.suggestions
    if (!s) return null
    const hasParams = Object.keys(s.params ?? {}).length > 0
    const hasRisk = Object.keys(s.risk_config ?? {}).length > 0
    return hasParams || hasRisk ? s : null
  }, [selected])

  const onClearUsage = async () => {
    try {
      await clearAiUsage()
      message.success('用量统计已清空')
      loadProfiles()
    } catch (err) {
      message.error(errDetail(err, '清空失败'))
    }
  }

  /** 把 AI 建议合并进原回测配置（直接回测 / 预填表单共用） */
  const buildMergedConfig = (): BacktestCreateRequest | null => {
    if (!suggestions || !baseConfig) return null
    return {
      ...baseConfig,
      name: `${baseConfig.name}-AI优化`,
      params: { ...(baseConfig.params ?? {}), ...(suggestions.params ?? {}) },
      risk_config: { ...(baseConfig.risk_config ?? {}), ...(suggestions.risk_config ?? {}) }
    }
  }

  /** 跳转回测中心预填表单（人工确认后再提交） */
  const applySuggestions = () => {
    const merged = buildMergedConfig()
    if (!merged) return
    navigate('/backtests', { state: { prefill: merged } })
  }

  /** 一键应用建议：直接创建下一轮回测任务（后端完整校验兜底），跳过表单确认 */
  const applyAndRun = async () => {
    const merged = buildMergedConfig()
    if (!merged) return
    setRunningDirect(true)
    try {
      const res = await createBacktest(merged)
      message.success('已应用建议并创建下一轮回测')
      navigate(`/backtests/${res.task_id}`)
    } catch (err) {
      message.error(errDetail(err, '应用建议并直接回测失败'))
    } finally {
      setRunningDirect(false)
    }
  }

  const suggestionRows = useMemo(() => {
    if (!suggestions) return []
    const rows: Array<{ group: string; key: string; label: string; oldV: unknown; newV: unknown }> = []
    Object.entries(suggestions.params ?? {}).forEach(([k, v]) => {
      rows.push({
        group: '策略参数',
        key: k,
        label: k,
        oldV: baseConfig?.params?.[k] ?? '-',
        newV: v
      })
    })
    Object.entries(suggestions.risk_config ?? {}).forEach(([k, v]) => {
      rows.push({
        group: '风控配置',
        key: k,
        label: RISK_LABELS[k] ?? k,
        oldV: (baseConfig?.risk_config as Record<string, unknown> | undefined)?.[k] ?? '-',
        newV: v
      })
    })
    return rows
  }, [suggestions, baseConfig])

  return (
    <Row gutter={16}>
      <Col span={7}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Card size="small" title="分析设置">
            {profilesResp && !hasAnyKey && (
              <Alert
                type="warning"
                showIcon
                message="未配置 LLM API Key"
                description={
                  <span>
                    请到「<a onClick={() => navigate('/keys')}>Key 管理</a>」页添加你的 API
                    Key（支持 DeepSeek / OpenRouter / 火山方舟 / 智谱等，可配多个自动切换）。
                  </span>
                }
                style={{ marginBottom: 16 }}
              />
            )}
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div>
                <Typography.Text type="secondary">回测任务（仅成功的）</Typography.Text>
                <Select
                  value={backtestId}
                  onChange={setBacktestId}
                  placeholder="选择回测任务"
                  style={{ width: '100%', marginTop: 4 }}
                  showSearch
                  optionFilterProp="label"
                  options={successBacktests.map((b) => ({
                    value: b.task_id,
                    label: `${b.name}（${b.task_id}）`
                  }))}
                />
              </div>
              <div>
                <Typography.Text type="secondary">使用的 Key</Typography.Text>
                <Select
                  value={profile}
                  onChange={setProfile}
                  style={{ width: '100%', marginTop: 4 }}
                  options={[
                    {
                      value: 'auto',
                      label: `自动轮换我的 Key 池（${userKeyPool.length} 个）${
                        userKeyPool.length === 0 && !hasAnyKey ? ' · 未配置' : ''
                      }`
                    },
                    ...userKeyPool.map((k) => ({
                      value: String(k.key_id),
                      label: `#${k.index} ${k.label || k.provider} · ${k.model}${
                        k.key_label ? ` · ${k.key_label}` : ''
                      }`
                    }))
                  ]}
                />
              </div>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                block
                loading={starting}
                disabled={!backtestId || !profile || !!currentTaskId}
                onClick={startAnalyze}
              >
                开始分析
              </Button>
              {currentTaskId && (
                <div>
                  <Progress percent={progress} status="active" />
                  <Typography.Text type="secondary">{taskMessage || '分析中...'}</Typography.Text>
                </div>
              )}
            </Space>
          </Card>

          <Card
            size="small"
            title="用量统计"
            extra={
              usage && usage.total_calls > 0 ? (
                <Popconfirm
                  title="清空用量统计？"
                  description="将删除全部历史统计记录（含测试数据），不可恢复"
                  onConfirm={onClearUsage}
                >
                  <Button type="link" size="small" danger>
                    清空
                  </Button>
                </Popconfirm>
              ) : null
            }
          >
            {usage ? (
              <>
                <Row gutter={12}>
                  <Col span={12}>
                    <Statistic title="总 Tokens" value={fmtInt(usage.total_tokens)} valueStyle={{ fontSize: 18 }} />
                  </Col>
                  <Col span={12}>
                    <Statistic title="总调用次数" value={fmtInt(usage.total_calls)} valueStyle={{ fontSize: 18 }} />
                  </Col>
                </Row>
                <div style={{ marginTop: 12 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    分 Profile 统计：
                  </Typography.Text>
                  {Object.entries(usage.by_profile ?? {}).length === 0 ? (
                    <Typography.Text type="secondary">暂无记录</Typography.Text>
                  ) : (
                    Object.entries(usage.by_profile).map(([name, u]) => (
                      <div key={name} style={{ marginTop: 6 }}>
                        <Tag>{name}</Tag>
                        <Typography.Text style={{ fontSize: 12 }}>
                          tokens {fmtInt(u.tokens)} · calls {u.calls}
                        </Typography.Text>
                      </div>
                    ))
                  )}
                </div>
              </>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无用量数据" />
            )}
            {stats && stats.total + stats.error > 0 && (
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  建议验证胜率（自动回测实测）：
                </Typography.Text>
                <div style={{ marginTop: 6 }}>
                  <Tag color="green">改善 {stats.改善}</Tag>
                  <Tag>持平 {stats.持平}</Tag>
                  <Tag color="red">恶化 {stats.恶化}</Tag>
                  {stats.error > 0 && <Tag>失败 {stats.error}</Tag>}
                  {stats.improved_rate != null && (
                    <Typography.Text style={{ fontSize: 12 }}>
                      改善率 {(stats.improved_rate * 100).toFixed(0)}%
                    </Typography.Text>
                  )}
                </div>
              </div>
            )}
          </Card>
        </Space>
      </Col>

      <Col span={17}>
        <Card
          size="small"
          title="分析结果"
          extra={
            analyses.length > 0 ? (
              <Select
                value={selectedAnalysisId ?? undefined}
                onChange={setSelectedAnalysisId}
                style={{ width: 320 }}
                options={analyses.map((a) => ({
                  value: a.task_id,
                  label: `${a.created_at?.slice(0, 19)?.replace('T', ' ') ?? ''} · ${a.profile}`
                }))}
              />
            ) : null
          }
        >
          <Spin spinning={loadingAnalyses}>
            {selected ? (
              selected.status === 'success' ? (
                <div>
                  <div className="md-content">
                    <ReactMarkdown>{selected.content ?? ''}</ReactMarkdown>
                  </div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    模型：{selected.model} · tokens: {selected.tokens_used ?? '-'} · 耗时：
                    {selected.elapsed ?? '-'}s
                  </Typography.Text>
                  {findings.length > 0 && (
                    <Card
                      size="small"
                      title={`系统诊断（规则引擎 · ${findings.length} 条，AI 仅负责解读）`}
                      style={{ marginTop: 16 }}
                    >
                      {findings.map((f) => (
                        <div
                          key={f.code}
                          style={{ padding: '6px 0', borderBottom: '1px dashed #f0f0f0' }}
                        >
                          <Space wrap size={6}>
                            <Tag color={SEVERITY_COLORS[f.severity]}>
                              {f.severity === 'high' ? '高' : f.severity === 'medium' ? '中' : '低'}
                            </Tag>
                            <Typography.Text strong>{f.title}</Typography.Text>
                            <Tag>{f.code}</Tag>
                          </Space>
                          <div style={{ fontSize: 12, marginTop: 2 }}>
                            <Typography.Text type="secondary">证据：{f.evidence}</Typography.Text>
                          </div>
                          <div style={{ fontSize: 12 }}>
                            <Typography.Text type="secondary">建议方向：{f.hint}</Typography.Text>
                          </div>
                        </div>
                      ))}
                    </Card>
                  )}
                  {suggestionRows.length > 0 && (
                    <Card
                      size="small"
                      title="AI 参数优化建议（可直接应用）"
                      style={{ marginTop: 16 }}
                      extra={
                        <Space>
                          <Button
                            type="primary"
                            size="small"
                            icon={<ThunderboltOutlined />}
                            onClick={applyAndRun}
                            loading={runningDirect}
                            disabled={!baseConfig}
                            title={
                              baseConfig
                                ? '合并建议并直接创建下一轮回测任务（跳过表单确认，后端完整校验兜底）'
                                : '正在获取原回测配置...'
                            }
                          >
                            应用并直接回测
                          </Button>
                          <Button
                            size="small"
                            onClick={applySuggestions}
                            disabled={!baseConfig}
                            title="跳转回测中心预填表单，人工确认后再提交"
                          >
                            预填表单确认
                          </Button>
                        </Space>
                      }
                    >
                      <div style={{ marginBottom: 8 }}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          建议值会合并进原回测配置：「应用并直接回测」跳过表单直接创建任务；
                          「预填表单确认」跳转回测中心，可人工修改后再提交。
                        </Typography.Text>
                      </div>
                      {suggestionRows.map((r) => (
                        <div
                          key={`${r.group}-${r.key}`}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            padding: '4px 0',
                            borderBottom: '1px dashed #f0f0f0'
                          }}
                        >
                          <Tag color={r.group === '策略参数' ? 'blue' : 'geekblue'}>
                            {r.group}
                          </Tag>
                          <Typography.Text style={{ minWidth: 140 }}>{r.label}</Typography.Text>
                          <Typography.Text type="secondary" delete>
                            {String(r.oldV)}
                          </Typography.Text>
                          <span>→</span>
                          <Typography.Text strong type="warning">
                            {String(r.newV)}
                          </Typography.Text>
                        </div>
                      ))}
                    </Card>
                  )}
                  {validation && (
                    <Card
                      size="small"
                      title="建议实测验证（同区间自动回测 A/B）"
                      style={{ marginTop: 16 }}
                    >
                      {validation.error ? (
                        <Alert
                          type="warning"
                          showIcon
                          message="验证回测未完成（不影响分析结论）"
                          description={validation.error}
                        />
                      ) : (
                        <>
                          <Space wrap align="center" style={{ marginBottom: 8 }}>
                            <Typography.Text type="secondary">实测结论：</Typography.Text>
                            <Tag
                              color={VERDICT_COLORS[validation.comparison?.verdict ?? ''] ?? 'default'}
                              style={{ fontSize: 14, padding: '2px 10px' }}
                            >
                              {validation.comparison?.verdict ?? '无结论'}
                            </Tag>
                            {(validation.comparison?.better?.length ?? 0) > 0 && (
                              <Typography.Text type="success" style={{ fontSize: 12 }}>
                                变好：
                                {validation.comparison?.better
                                  ?.map((k) => METRIC_LABELS[k] ?? k)
                                  .join('、')}
                              </Typography.Text>
                            )}
                            {(validation.comparison?.worse?.length ?? 0) > 0 && (
                              <Typography.Text type="danger" style={{ fontSize: 12 }}>
                                变差：
                                {validation.comparison?.worse
                                  ?.map((k) => METRIC_LABELS[k] ?? k)
                                  .join('、')}
                              </Typography.Text>
                            )}
                          </Space>
                          {(validation.comparison?.rows ?? []).map((r) => (
                            <div
                              key={r.key}
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: 8,
                                padding: '4px 0',
                                borderBottom: '1px dashed #f0f0f0'
                              }}
                            >
                              <Tag color="geekblue">{METRIC_LABELS[r.key] ?? r.key}</Tag>
                              <Typography.Text type="secondary">
                                {fmtMetric(r.key, r.orig)}
                              </Typography.Text>
                              <span>→</span>
                              <Typography.Text strong>{fmtMetric(r.key, r.new)}</Typography.Text>
                              <Typography.Text
                                type={
                                  r.better === true ? 'success' : r.better === false ? 'danger' : 'secondary'
                                }
                                style={{ fontSize: 12 }}
                              >
                                Δ {fmtMetric(r.key, r.delta)}
                              </Typography.Text>
                            </div>
                          ))}
                          {validation.commentary && (
                            <div
                              style={{
                                marginTop: 12,
                                padding: '8px 12px',
                                background: '#fafafa',
                                borderRadius: 6
                              }}
                            >
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                AI 复核点评（基于实测数字）：
                              </Typography.Text>
                              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>
                                {validation.commentary}
                              </div>
                            </div>
                          )}
                        </>
                      )}
                    </Card>
                  )}
                </div>
              ) : selected.status === 'failed' ? (
                <Alert type="error" showIcon message="分析失败" description={selected.error || '无详细错误信息'} />
              ) : (
                <div style={{ padding: 24 }}>
                  <Progress percent={progress} status="active" />
                  <Typography.Text type="secondary">
                    {taskMessage || '分析任务进行中，请稍候...'}
                  </Typography.Text>
                </div>
              )
            ) : (
              <Empty
                description={
                  backtestId ? '该回测暂无分析记录，点击左侧"开始分析"生成' : '请先选择回测任务'
                }
              />
            )}
          </Spin>
        </Card>
      </Col>
    </Row>
  )
}
