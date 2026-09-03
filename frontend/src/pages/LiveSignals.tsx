import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Button, Card, Col, Collapse, InputNumber, Modal, Popconfirm, Progress,
  Row, Select, Space, Statistic, Switch, Table, Tag, Typography, message
} from 'antd'
import {
  CheckCircleOutlined, CloseCircleOutlined, DeleteOutlined, NotificationOutlined,
  PlayCircleOutlined, SaveOutlined, SyncOutlined, ThunderboltOutlined
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type {
  IntradayCodeStatus, IntradayRunResult, IntradayStatus, LiveConfig,
  LivePosition, LiveSignalItem, ReadinessResult, ShadowStats, SlippageResult,
  TaskStatus
} from '../api/types'
import {
  addLiveFill, getIntradayStatus, getLiveSummary, getReadiness, getShadowStats,
  getSlippage, resetLiveData, runIntraday, runMorning, runPostclose,
  saveLiveConfig, setLiveSignalStatus, syncLivePositions
} from '../api/client'
import { useTaskProgress } from '../hooks/useTaskProgress'
import { fmtMoney } from '../utils/format'

const STYPE_TAG: Record<string, string> = {
  开仓: 'red', 加仓: 'orange', 做T: 'purple', 止损: 'green',
  止盈: 'cyan', 减仓: 'lime', 清仓: 'default', 预警: 'volcano',
  池子: 'geekblue', 对账: 'blue'
}

const RANK_KEY_OPTS = [
  { value: 'score', label: '累计强度（score）' },
  { value: 'accel', label: '加速度（accel）' },
  { value: 'fresh', label: '金叉新鲜（fresh）' },
  { value: 'mom_gap', label: '短中差值（mom_gap）' }
]

const INDEX_OPTS = [
  { value: 'zz500', label: '中证500' },
  { value: 'hs300', label: '沪深300' },
  { value: 'csi1000', label: '中证1000' },
  { value: 'kcb50', label: '科创50' }
]

const BOARD_OPTS = [
  { value: 'kcb', label: '科创板' },
  { value: 'cyb', label: '创业板' },
  { value: 'zxb', label: '中小板/主板' }
]

const T_MODE_OPTS = [
  { value: 'off', label: '关闭做T（off，M2 起步建议）' },
  { value: 'grid', label: '网格双止损（grid）' },
  { value: 'discipline', label: '回补纪律（discipline）' },
  { value: 'time', label: '时点规律T（time）' }
]

function statusTag(s: string) {
  if (s === '已成交') return <Tag color="success">已成交</Tag>
  if (s === '已忽略') return <Tag>已忽略</Tag>
  if (s === '已过期') return <Tag color="default">已过期</Tag>
  if (s === '信息') return <Tag color="blue">信息</Tag>
  return <Tag color="processing">待执行</Tag>
}

function staleDays(asOf: string | null | undefined): number {
  if (!asOf) return 0
  return Math.floor((Date.now() - new Date(asOf).getTime()) / 86400000)
}

export default function LiveSignals() {
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof getLiveSummary>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [premarketLoading, setPremarketLoading] = useState(false)
  const [fillTarget, setFillTarget] = useState<LiveSignalItem | null>(null)
  const [fillPrice, setFillPrice] = useState<number | null>(null)
  const [fillVolume, setFillVolume] = useState<number | null>(null)
  const [cfg, setCfg] = useState<LiveConfig | null>(null)
  const [cfgSaving, setCfgSaving] = useState(false)
  const [morningLoading, setMorningLoading] = useState(false)
  const [intradayStatus, setIntradayStatus] = useState<IntradayStatus | null>(null)
  const [intradayRun, setIntradayRun] = useState<IntradayRunResult | null>(null)
  const [intradayLoading, setIntradayLoading] = useState(false)
  const [autoPoll, setAutoPoll] = useState(false)
  const [postcloseLoading, setPostcloseLoading] = useState(false)
  const [slip, setSlip] = useState<SlippageResult | null>(null)
  const [shadow, setShadow] = useState<ShadowStats | null>(null)
  const [ready, setReady] = useState<ReadinessResult | null>(null)
  const loadedReports = useRef<Set<string>>(new Set())

  const loadStatus = useCallback(() => {
    getIntradayStatus().then(setIntradayStatus).catch(() => {})
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  useEffect(() => {
    if (!autoPoll) return
    const t = setInterval(async () => {
      try { await runIntraday() } catch { /* 断流由后端熔断推送告警 */ }
      loadStatus()
    }, 60_000)
    return () => clearInterval(t)
  }, [autoPoll, loadStatus])

  const loadReport = (key: string) => {
    if (loadedReports.current.has(key)) return
    loadedReports.current.add(key)
    if (key === 'slip') getSlippage().then(setSlip).catch(() => {})
    if (key === 'shadow') getShadowStats().then(setShadow).catch(() => {})
    if (key === 'ready') getReadiness().then(setReady).catch(() => {})
  }

  const onCollapseChange = (keys: string | string[]) => {
    const ks = Array.isArray(keys) ? keys : [keys]
    ks.forEach(loadReport)
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const s = await getLiveSummary()
      setSummary(s)
      setCfg((prev) => prev ?? s.config)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // 盘前编排任务进度（提交后在本页直接跟踪，无需跳转）
  const [morningTaskId, setMorningTaskId] = useState<string | null>(null)
  const onMorningDone = useCallback((status: TaskStatus,
                                     full: { message: string } | null) => {
    if (status === 'success') {
      message.success('盘前流程已完成并推送，信号/池子已落库')
      refresh()
      loadStatus()
    } else if (status === 'failed') {
      message.error(`盘前流程失败：${full?.message || '未知错误'}`)
    }
    setMorningTaskId(null)
  }, [refresh, loadStatus])
  const morningTask = useTaskProgress(morningTaskId, onMorningDone)

  const onFill = async () => {
    if (!fillTarget || !fillPrice || !fillVolume) return
    try {
      await addLiveFill({
        signal_id: fillTarget.id,
        code: fillTarget.code || '',
        side: ['开仓', '加仓'].includes(fillTarget.stype) ? 'buy' : 'sell',
        fill_price: fillPrice, fill_volume: fillVolume
      })
      message.success('成交已回填，虚拟持仓已更新')
      setFillTarget(null)
      await refresh()
    } catch (err) {
      message.error((err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || '回填失败')
    }
  }

  const onIgnore = async (s: LiveSignalItem) => {
    await setLiveSignalStatus(s.id, '已忽略')
    await refresh()
  }

  const onSync = async () => {
    if (!summary) return
    await syncLivePositions(
      summary.positions.map((p: LivePosition) => ({
        code: p.code, name: p.name, volume: p.volume, cost_price: p.cost_price
      })))
    message.success('已按当前虚拟持仓校准')
    await refresh()
  }

  const onSaveCfg = async () => {
    if (!cfg) return
    setCfgSaving(true)
    try {
      await saveLiveConfig(cfg)
      message.success('配置已保存，下次盘前流程生效')
      await refresh()
    } finally {
      setCfgSaving(false)
    }
  }

  const onReset = async () => {
    await resetLiveData()
    message.success('已清空信号数据（流程参数配置保留）')
    await refresh()
  }

  const onMorning = async (updateData: boolean) => {
    setMorningLoading(true)
    try {
      const r = await runMorning(updateData)
      setMorningTaskId(r.task_id)
      message.success(
        `盘前编排任务已提交（${r.task_id}）${updateData ? '：先做全市场日线增量更新（约数分钟），' : ''}` +
        '完成后自动执行盘前流程并推送，进度见下方')
    } catch (err) {
      message.error((err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || '盘前编排提交失败')
    } finally {
      setMorningLoading(false)
    }
  }

  const onIntraday = async () => {
    setIntradayLoading(true)
    try {
      const r = await runIntraday()
      setIntradayRun(r)
      if (r.skipped) {
        message.info(r.skipped)
      } else if (r.signals.length) {
        message.success(
          `本轮 ${r.signals.length} 条信号（喂入 ${r.fed_bars} 根完成 bar），` +
          `推送${r.pushed ? '成功' : '未配置飞书（仅落库）'}`)
      } else {
        message.info(`本轮无信号（喂入 ${r.fed_bars} 根完成 bar）`)
      }
      loadStatus()
      await refresh()
    } catch (err) {
      message.error((err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || '盘中轮询失败')
    } finally {
      setIntradayLoading(false)
    }
  }

  const onPostclose = async () => {
    setPostcloseLoading(true)
    try {
      const r = await runPostclose()
      message.success(
        `盘后完成：分钟线落库 ${r.saved.length} 只${r.skipped.length ? `（失败 ${r.skipped.length}）` : ''}，` +
        `对账卡推送${r.pushed ? '成功' : '未配置飞书'}`)
      await refresh()
    } catch (err) {
      message.error((err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || '盘后流程失败')
    } finally {
      setPostcloseLoading(false)
    }
  }

  const signalCols: ColumnsType<LiveSignalItem> = [
    {
      title: '类型', dataIndex: 'stype', width: 80,
      render: (v: string) => <Tag color={STYPE_TAG[v] ?? 'default'}>{v}</Tag>
    },
    { title: '代码', dataIndex: 'code', width: 90, render: (v) => v || '-' },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    { title: '理由', dataIndex: 'reason', ellipsis: true },
    {
      title: '建议金额', dataIndex: 'suggest_amount', width: 110, align: 'right',
      render: (v) => (v != null ? fmtMoney(v) : '-')
    },
    {
      title: '参考价', dataIndex: 'ref_price', width: 90, align: 'right',
      render: (v) => (v != null ? v.toFixed(3) : '-')
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v: string) => statusTag(v)
    },
    { title: '时间', dataIndex: 'ts', width: 160 },
    {
      title: '操作', width: 150,
      render: (_v, s) =>
        s.status === '待执行' && s.code ? (
          <Space size={4}>
            <Button size="small" type="primary" onClick={() => {
              setFillTarget(s)
              setFillPrice(s.ref_price)
              setFillVolume(null)
            }}>回填</Button>
            <Button size="small" onClick={() => onIgnore(s)}>忽略</Button>
          </Space>
        ) : '-'
    }
  ]

  const posCols: ColumnsType<LivePosition> = [
    { title: '代码', dataIndex: 'code', width: 90 },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '数量', dataIndex: 'volume', width: 100, align: 'right',
      render: (v) => v.toLocaleString('zh-CN')
    },
    {
      title: '成本价', dataIndex: 'cost_price', width: 90, align: 'right',
      render: (v) => v?.toFixed(3)
    },
    {
      title: '开仓日', dataIndex: 'open_day', width: 110,
      render: (v) => v || '-'
    }
  ]

  const intradayCols: ColumnsType<IntradayCodeStatus> = [
    { title: '代码', dataIndex: 'code', width: 90 },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '现价', dataIndex: 'price', width: 90, align: 'right',
      render: (v) => (v != null ? v.toFixed(3) : '-')
    },
    {
      title: '来源', dataIndex: 'in_pool', width: 90,
      render: (v, r) => (
        <Space size={4}>
          {r.held && <Tag color="orange">持仓</Tag>}
          {v && <Tag color="geekblue">池子</Tag>}
          {!r.held && !v && <Tag>跟踪</Tag>}
        </Space>
      )
    },
    {
      title: '状态机', width: 160,
      render: (_v, r) => (
        <Typography.Text style={{ fontSize: 12 }}>
          {r.opened ? (r.full ? `满配·已加${r.adds_done}` : '试仓') : '未持仓'}
          {r.exit_stage > 0 ? `｜退出中(${r.exit_stage})` : ''}
        </Typography.Text>
      )
    },
    {
      title: '喂bar游标', dataIndex: 'last_bar', width: 150,
      render: (v) => <Typography.Text style={{ fontSize: 12 }}>{v || '-'}</Typography.Text>
    }
  ]

  const poolCols: ColumnsType<{ code: string; name?: string }> = [
    { title: '代码', dataIndex: 'code', width: 100 },
    { title: '名称', dataIndex: 'name', ellipsis: true }
  ]

  const pool = summary?.pool
  const stale = staleDays(pool?.as_of)
  const numCell = (v: number | null | undefined, onChange: (v: number | null) => void,
                   step: number, min: number) => (
    <InputNumber style={{ width: '100%' }} value={v ?? undefined}
      step={step} min={min} onChange={(v) => onChange(v)} />
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {stale > 4 && (
        <Alert
          type="warning" showIcon
          message={`行情数据截至 ${pool?.as_of}（滞后 ${stale} 天）`}
          description="盘前流程只读现有日线库、不自动拉数据。数据不完整会导致选股出现'幸存者偏差'（只有数据完整的票参选）。请先到数据管理页更新日线，再执行盘前流程。"
        />
      )}
      <Row gutter={16}>
        <Col span={6}>
          <Card size="small" loading={loading}>
            <Statistic title="池级 gate"
              value={pool?.gate_state ? '停开仓' : '正常'}
              valueStyle={{ fontSize: 20, color: pool?.gate_state ? '#cf1322' : '#3f8600' }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              健康度 {pool?.health_history?.slice(-1)[0]?.health ?? '-'}
            </Typography.Text>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={loading}>
            <Statistic title="当前池子" value={`${pool?.pool?.length ?? 0} 只`}
              valueStyle={{ fontSize: 20 }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              基准日 {pool?.as_of ?? '-'}｜候选域：{cfg?.auto_index?.length ? cfg.auto_index.join('+') : '全市场'}
            </Typography.Text>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={loading}>
            <Statistic title="虚拟持仓" value={`${summary?.positions?.length ?? 0} 只`}
              valueStyle={{ fontSize: 20 }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              空仓起始 {pool?.idle_start ?? '-'}
            </Typography.Text>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={loading}>
            <Statistic title="飞书推送"
              value={summary?.feishu_configured ? '已配置' : '未配置'}
              valueStyle={{ fontSize: 20,
                color: summary?.feishu_configured ? '#3f8600' : '#cf1322' }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              .env 的 FEISHU_WEBHOOK_URL
            </Typography.Text>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="每日信号流程（盘前 → 盘中 → 盘后）"
        extra={
          <Space>
            <Button icon={<PlayCircleOutlined />} loading={morningLoading}
              onClick={() => onMorning(false)}>
              仅盘前（不拉数据）
            </Button>
            <Button type="primary" icon={<SyncOutlined />} loading={morningLoading}
              onClick={() => onMorning(true)}>
              盘前流程（自动拉数据）
            </Button>
            <Button icon={<ThunderboltOutlined />} loading={postcloseLoading}
              onClick={onPostclose}>
              盘后落库与对账
            </Button>
          </Space>
        }>
        <Alert
          type="info" showIcon
          message={
            '盘前：日线增量更新（含完整性守卫）→ T-1 特征重算 → 池级 gate → 空仓重选 → 退出检查 → 飞书推送；' +
            '盘中：完成 bar → 状态机步进 → 风控前置 → 推送（下方控制台）；' +
            '盘后：分钟线落库 + 对账卡推送'}
          style={{ marginBottom: 8 }}
        />
        {summary?.signals?.length ? (
          <Alert
            type={summary.signals.some((s) => s.stype === '开仓') ? 'warning' : 'success'}
            showIcon icon={<NotificationOutlined />}
            message={`最近一次盘前产出 ${summary.signals.length} 条交易信号（见下方列表）`}
          />
        ) : (
          <Typography.Text type="secondary">尚未产生交易信号</Typography.Text>
        )}
        {morningTaskId && (
          <div style={{ marginTop: 8 }}>
            <Progress
              percent={Math.round(morningTask.progress)}
              size="small" status="active"
              strokeColor={{ from: '#1677ff', to: '#36cfc9' }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              任务 {morningTaskId}｜{morningTask.message || '排队中...'}
            </Typography.Text>
          </div>
        )}
      </Card>

      <Card
        size="small"
        title={
          <Space>
            <span>盘中信号机（M2）</span>
            <Tag color={intradayStatus?.session ? 'green' : 'default'}>
              {intradayStatus?.session ? '盘中时段' : '非交易时段'}
            </Tag>
            {intradayStatus && (
              <Tag color={intradayStatus.t_mode === 'off' ? 'default' : 'purple'}>
                t_mode={intradayStatus.t_mode}
              </Tag>
            )}
            {intradayStatus?.heartbeat?.alerted && (
              <Tag color="volcano">断流熔断中</Tag>
            )}
          </Space>
        }
        extra={
          <Space>
            <Button size="small" type="primary" icon={<ThunderboltOutlined />}
              loading={intradayLoading} onClick={onIntraday}>
              执行盘中轮询
            </Button>
            <Space size={4}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                自动(60秒)
              </Typography.Text>
              <Switch size="small" checked={autoPoll} onChange={setAutoPoll} />
            </Space>
          </Space>
        }>
        <Table<IntradayCodeStatus>
          rowKey="code" size="small"
          dataSource={intradayStatus?.codes ?? []}
          columns={intradayCols}
          pagination={false}
          locale={{ emptyText: '无跟踪标的（先执行盘前流程生成池子）' }}
        />
        {intradayRun?.suspended?.length ? (
          <Alert type="warning" showIcon style={{ marginTop: 8 }}
            message="本轮拦截/暂停"
            description={intradayRun.suspended
              .map((w) => `${w.code}：${w.reason}`)
              .join('；')} />
        ) : null}
      </Card>

      <Card size="small" title="信号列表">
        <Table<LiveSignalItem>
          rowKey="id" size="small" loading={loading}
          dataSource={summary?.signals ?? []}
          columns={signalCols}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
          title={() => (
            <Popconfirm
              title="清空全部信号数据？"
              description="将删除：信号流水、成交回填、虚拟持仓、池子状态。流程参数配置保留。"
              okText="清空重来" okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={onReset}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                清空重来
              </Button>
            </Popconfirm>
          )}
        />
      </Card>

      <Card size="small" title="虚拟持仓"
        extra={
          <Button size="small" icon={<SyncOutlined />} onClick={onSync}>
            对账校准（以当前系统持仓为准）
          </Button>
        }>
        <Table<LivePosition>
          rowKey="code" size="small" loading={loading}
          dataSource={summary?.positions ?? []}
          columns={posCols}
          pagination={false}
          locale={{ emptyText: '暂无虚拟持仓（回填买入成交后生成）' }}
        />
      </Card>

      <Collapse onChange={onCollapseChange}
        items={[
          {
            key: 'pool', label: `当前池子成员（${pool?.pool?.length ?? 0} 只，基准日 ${pool?.as_of ?? '-'}）`,
            children: (
              <Table
                rowKey="code" size="small"
                dataSource={pool?.pool ?? []}
                columns={poolCols}
                pagination={false}
                locale={{ emptyText: '暂无池子（执行盘前流程后生成）' }}
              />
            )
          },
          {
            key: 'cfg', label: '流程参数配置（保存后下次盘前流程生效）',
            children: cfg ? (
              <div>
                <Row gutter={12}>
                  <Col span={8}>
                    <Typography.Text type="secondary">候选域（指数成分并集）</Typography.Text>
                    <Select mode="multiple" style={{ width: '100%' }} allowClear
                      value={cfg.auto_index} options={INDEX_OPTS}
                      onChange={(v) => setCfg({ ...cfg, auto_index: v })} />
                  </Col>
                  <Col span={8}>
                    <Typography.Text type="secondary">板块过滤（空=不限）</Typography.Text>
                    <Select mode="multiple" style={{ width: '100%' }} allowClear
                      value={cfg.auto_boards} options={BOARD_OPTS}
                      onChange={(v) => setCfg({ ...cfg, auto_boards: v })} />
                  </Col>
                  <Col span={8}>
                    <Typography.Text type="secondary">选股排序键（rank_key）</Typography.Text>
                    <Select style={{ width: '100%' }} value={cfg.rank_key}
                      options={RANK_KEY_OPTS}
                      onChange={(v) => setCfg({ ...cfg, rank_key: v })} />
                  </Col>
                </Row>
                <Row gutter={12} style={{ marginTop: 8 }}>
                  <Col span={6}>
                    <Typography.Text type="secondary">虚拟资金（initial_capital）</Typography.Text>
                    {numCell(cfg.initial_capital,
                      (v) => setCfg({ ...cfg, initial_capital: v ?? 3000000 }),
                      100000, 10000)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">单票建议比例（suggest_pct）</Typography.Text>
                    {numCell(cfg.suggest_pct,
                      (v) => setCfg({ ...cfg, suggest_pct: v ?? 0.15 }),
                      0.01, 0.01)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">池子大小（top_x）</Typography.Text>
                    {numCell(cfg.top_x, (v) => setCfg({ ...cfg, top_x: v ?? 30 }),
                      1, 1)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">空仓重选天数（auto_idle_days）</Typography.Text>
                    {numCell(cfg.auto_idle_days,
                      (v) => setCfg({ ...cfg, auto_idle_days: v ?? 5 }), 1, 1)}
                  </Col>
                </Row>
                <Row gutter={12} style={{ marginTop: 8 }}>
                  <Col span={6}>
                    <Typography.Text type="secondary">衰退信号阈值（exit_need）</Typography.Text>
                    {numCell(cfg.exit_need, (v) => setCfg({ ...cfg, exit_need: v ?? 2 }),
                      1, 1)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">gate 触发阈值（enter_th）</Typography.Text>
                    {numCell(cfg.enter_th, (v) => setCfg({ ...cfg, enter_th: v ?? 0.15 }),
                      0.01, 0.01)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">站上均线周期（above_ma）</Typography.Text>
                    {numCell(cfg.above_ma, (v) => setCfg({ ...cfg, above_ma: v ?? 20 }),
                      5, 5)}
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">榜单容量（pool_n）</Typography.Text>
                    {numCell(cfg.pool_n, (v) => setCfg({ ...cfg, pool_n: v ?? 6 }), 1, 1)}
                  </Col>
                </Row>
                <Row gutter={12} style={{ marginTop: 8 }}>
                  <Col span={6}>
                    <Typography.Text type="secondary">做T机制（t_mode）</Typography.Text>
                    <Select style={{ width: '100%' }} value={cfg.t_mode || 'off'}
                      options={T_MODE_OPTS}
                      onChange={(v) => setCfg({ ...cfg, t_mode: v })} />
                  </Col>
                  <Col span={6}>
                    <Typography.Text type="secondary">最大持仓只数（max_holdings）</Typography.Text>
                    {numCell(cfg.max_holdings ?? 3,
                      (v) => setCfg({ ...cfg, max_holdings: v ?? 3 }), 1, 1)}
                  </Col>
                </Row>
                <Button type="primary" icon={<SaveOutlined />} loading={cfgSaving}
                  onClick={onSaveCfg} style={{ marginTop: 12 }}>
                  保存配置
                </Button>
              </div>
            ) : null
          },
          {
            key: 'slip',
            label: '滑点统计（M3：实际成交价 vs 信号参考价，正=不利成本）',
            children: slip ? (
              <div>
                <Space size="large" style={{ marginBottom: 8 }}>
                  <Statistic title="样本数" value={slip.summary.n} />
                  <Statistic title="平均滑点成本"
                    value={slip.summary.avg_slip_pct != null
                      ? `${slip.summary.avg_slip_pct}%` : '-'} />
                  <Statistic title="买入均值"
                    value={slip.summary.buy_avg_slip_pct != null
                      ? `${slip.summary.buy_avg_slip_pct}%` : '-'} />
                  <Statistic title="卖出均值"
                    value={slip.summary.sell_avg_slip_pct != null
                      ? `${slip.summary.sell_avg_slip_pct}%` : '-'} />
                </Space>
                <Table
                  rowKey="fill_id" size="small"
                  dataSource={slip.rows}
                  pagination={{ pageSize: 10 }}
                  columns={[
                    { title: '代码', dataIndex: 'code', width: 90 },
                    { title: '类型', dataIndex: 'stype', width: 80 },
                    { title: '方向', dataIndex: 'side', width: 70 },
                    { title: '参考价', dataIndex: 'ref_price', width: 90, align: 'right',
                      render: (v) => v?.toFixed(3) },
                    { title: '成交价', dataIndex: 'fill_price', width: 90, align: 'right',
                      render: (v) => v?.toFixed(3) },
                    { title: '滑点%', dataIndex: 'slip_pct', width: 90, align: 'right',
                      render: (v) => (
                        <Typography.Text type={v > 0 ? 'danger' : 'success'}>
                          {v}%
                        </Typography.Text>
                      ) },
                    { title: '时间', dataIndex: 'fill_time' }
                  ]}
                  locale={{ emptyText: '暂无回填成交——回填后自动积累滑点样本' }}
                />
              </div>
            ) : <Typography.Text type="secondary">加载中...</Typography.Text>
          },
          {
            key: 'shadow',
            label: '影子运行（M3：假设每条信号都按参考价足额执行 vs 实际回填）',
            children: shadow ? (
              <Space size="large" wrap>
                <Statistic title="信号数" value={shadow.n_signals} />
                <Statistic title="已执行" value={shadow.n_filled}
                  suffix={`/ ${shadow.n_signals}`} />
                <Statistic title="执行率"
                  value={shadow.fill_rate != null
                    ? `${(shadow.fill_rate * 100).toFixed(1)}%` : '-'} />
                <Statistic title="影子已实现盈亏（按参考价）"
                  valueStyle={{ color: shadow.shadow_pnl >= 0 ? '#3f8600' : '#cf1322' }}
                  value={fmtMoney(shadow.shadow_pnl)} />
                <Statistic title="实际已实现盈亏（回填口径）"
                  valueStyle={{ color: shadow.actual_pnl >= 0 ? '#3f8600' : '#cf1322' }}
                  value={fmtMoney(shadow.actual_pnl)} />
                <Statistic title="执行差（实际-影子）"
                  valueStyle={{ color: shadow.gap_pnl >= 0 ? '#3f8600' : '#cf1322' }}
                  value={fmtMoney(shadow.gap_pnl)} />
                <Statistic title="影子天数" value={shadow.days} />
              </Space>
            ) : <Typography.Text type="secondary">加载中...</Typography.Text>
          },
          {
            key: 'ready',
            label: 'M4 小资金实盘就绪检查',
            children: ready ? (
              <div>
                <Alert
                  type={ready.ready ? 'success' : 'warning'} showIcon
                  style={{ marginBottom: 8 }}
                  message={ready.ready
                    ? '全部通过——可按 M4 流程以极小资金（5 万级）开始跟单灰度'
                    : '存在未通过项——建议先补齐再上小资金'}
                />
                {ready.items.map((it) => (
                  <Space key={it.key} size={8} style={{ display: 'flex', marginBottom: 4 }}>
                    {it.ok
                      ? <CheckCircleOutlined style={{ color: '#3f8600' }} />
                      : <CloseCircleOutlined style={{ color: '#cf1322' }} />}
                    <Typography.Text strong>{it.label}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {it.detail}
                    </Typography.Text>
                  </Space>
                ))}
              </div>
            ) : <Typography.Text type="secondary">加载中（含行情源探测，约 1-2 秒）...</Typography.Text>
          }
        ]}
      />

      <Modal
        title={`回填成交：${fillTarget?.code ?? ''} ${fillTarget?.name ?? ''} ${fillTarget?.stype ?? ''}`}
        open={!!fillTarget}
        onOk={onFill}
        onCancel={() => setFillTarget(null)}
        okText="确认回填"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            {fillTarget?.reason}｜建议金额 {fillTarget?.suggest_amount != null
              ? fmtMoney(fillTarget.suggest_amount) : '-'}
            ｜参考价 {fillTarget?.ref_price != null ? fillTarget.ref_price.toFixed(3) : '-'}
          </Typography.Text>
          <Typography.Text>成交价：</Typography.Text>
          <InputNumber
            min={0.001} step={0.001} style={{ width: '100%' }}
            value={fillPrice} onChange={(v) => setFillPrice(v)}
            placeholder="实际成交价" />
          <Typography.Text>数量（股）：</Typography.Text>
          <InputNumber
            min={100} step={100} style={{ width: '100%' }}
            value={fillVolume} onChange={(v) => setFillVolume(v)}
            placeholder="实际成交数量" />
        </Space>
      </Modal>
    </Space>
  )
}
