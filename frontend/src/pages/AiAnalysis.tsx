import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  message,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography
} from 'antd'
import { PlayCircleOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import {
  errDetail,
  getAiAnalyses,
  getAiProfiles,
  getBacktests,
  startAiAnalyze
} from '../api/client'
import type { AiAnalysisItem, AiProfilesResponse, BacktestListItem } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import { fmtInt } from '../utils/format'

export default function AiAnalysis() {
  const [backtests, setBacktests] = useState<BacktestListItem[]>([])
  const [profilesResp, setProfilesResp] = useState<AiProfilesResponse | null>(null)
  const [backtestId, setBacktestId] = useState<string | undefined>(undefined)
  const [profile, setProfile] = useState<string | undefined>(undefined)
  const [analyses, setAnalyses] = useState<AiAnalysisItem[]>([])
  const [selectedAnalysisId, setSelectedAnalysisId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null)
  const [loadingAnalyses, setLoadingAnalyses] = useState(false)

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
  const availableProfiles = useMemo(
    () => (profilesResp?.profiles ?? []).filter((p) => p.available),
    [profilesResp]
  )

  useEffect(() => {
    if (!profile && profilesResp?.default && availableProfiles.some((p) => p.name === profilesResp.default)) {
      setProfile(profilesResp.default)
    }
  }, [profilesResp, availableProfiles, profile])

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
    } else {
      setAnalyses([])
      setSelectedAnalysisId(null)
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
  })

  const startAnalyze = async () => {
    if (!backtestId) {
      message.warning('请选择回测任务')
      return
    }
    if (!profile) {
      message.warning('请选择分析 Profile')
      return
    }
    setStarting(true)
    try {
      const res = await startAiAnalyze({ backtest_id: backtestId, profile })
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

  return (
    <Row gutter={16}>
      <Col span={7}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Card size="small" title="分析设置">
            {profilesResp && availableProfiles.length === 0 && (
              <Alert
                type="warning"
                showIcon
                message="未配置 LLM API Key"
                description="当前没有可用的 AI Profile，请在服务端配置对应环境变量后再使用 AI 分析。"
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
                <Typography.Text type="secondary">AI Profile</Typography.Text>
                <Select
                  value={profile}
                  onChange={setProfile}
                  placeholder="选择 Profile"
                  style={{ width: '100%', marginTop: 4 }}
                  options={availableProfiles.map((p) => ({
                    value: p.name,
                    label: (
                      <Space>
                        <span>
                          {p.name} · {p.model}
                        </span>
                        {profilesResp?.default === p.name && <Tag color="blue">默认</Tag>}
                      </Space>
                    )
                  }))}
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

          <Card size="small" title="用量统计">
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
