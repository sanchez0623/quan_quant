import { useCallback, useEffect, useState } from 'react'
import {
  Alert, Button, Card, Col, InputNumber, Modal, Row, Space, Statistic, Table,
  Tag, Typography, message
} from 'antd'
import { NotificationOutlined, SyncOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type { LivePosition, LiveSignalItem } from '../api/types'
import {
  addLiveFill, getLiveSummary, runPremarket, setLiveSignalStatus,
  syncLivePositions
} from '../api/client'
import { fmtMoney } from '../utils/format'

const STYPE_TAG: Record<string, string> = {
  开仓: 'red', 加仓: 'orange', 做T: 'purple', 止损: 'green',
  止盈: 'cyan', 减仓: 'lime', 清仓: 'default', 预警: 'volcano',
  池子: 'geekblue', 对账: 'blue'
}

function statusTag(s: string) {
  if (s === '已成交') return <Tag color="success">已成交</Tag>
  if (s === '已忽略') return <Tag>已忽略</Tag>
  if (s === '已过期') return <Tag color="default">已过期</Tag>
  if (s === '信息') return <Tag color="blue">信息</Tag>
  return <Tag color="processing">待执行</Tag>
}

export default function LiveSignals() {
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof getLiveSummary>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [premarketLoading, setPremarketLoading] = useState(false)
  const [fillTarget, setFillTarget] = useState<LiveSignalItem | null>(null)
  const [fillPrice, setFillPrice] = useState<number | null>(null)
  const [fillVolume, setFillVolume] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setSummary(await getLiveSummary())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const onPremarket = async () => {
    setPremarketLoading(true)
    try {
      const r = await runPremarket()
      message.success(
        `盘前流程完成：${r.rebalanced ? `重选新池 ${r.pool.length} 只` : '未触发重选'}；` +
        `推送${r.pushed ? '成功' : '未配置（仅落库）'}`)
      await refresh()
    } catch (err) {
      message.error((err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || '盘前流程失败')
    } finally {
      setPremarketLoading(false)
    }
  }

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
    // 以系统虚拟持仓为准回写校准（M1 简化：对账差异人工核对后点此刷新状态）
    await syncLivePositions(
      summary.positions.map((p: LivePosition) => ({
        code: p.code, name: p.name, volume: p.volume, cost_price: p.cost_price
      })))
    message.success('已按当前虚拟持仓校准')
    await refresh()
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
    { title: '数量', dataIndex: 'volume', width: 100, align: 'right',
      render: (v) => v.toLocaleString('zh-CN') },
    { title: '成本价', dataIndex: 'cost_price', width: 90, align: 'right',
      render: (v) => v?.toFixed(3) },
    { title: '开仓日', dataIndex: 'open_day', width: 110,
      render: (v) => v || '-' }
  ]

  const pool = summary?.pool
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
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
              基准日 {pool?.as_of ?? '-'}
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

      <Card size="small" title="盘前信号流程"
        extra={
          <Button type="primary" icon={<SyncOutlined />}
            loading={premarketLoading} onClick={onPremarket}>
            立即执行盘前流程
          </Button>
        }>
        <Alert
          type="info" showIcon
          message="T-1 特征重算 → 池级健康度/gate → 空仓重选判定 → 持仓退出检查 → 飞书推送 + 落库"
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
      </Card>

      <Card size="small" title="信号列表">
        <Table<LiveSignalItem>
          rowKey="id" size="small" loading={loading}
          dataSource={summary?.signals ?? []}
          columns={signalCols}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
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

      <Modal
        title={`回填成交：${fillTarget?.code ?? ''} ${fillTarget?.stype ?? ''}`}
        open={!!fillTarget}
        onOk={onFill}
        onCancel={() => setFillTarget(null)}
        okText="确认回填"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            {fillTarget?.reason}｜建议金额 {fillTarget?.suggest_amount != null
              ? fmtMoney(fillTarget.suggest_amount) : '-'}
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
