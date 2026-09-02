import { useMemo, useState } from 'react'
import { Button, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { DownloadOutlined } from '@ant-design/icons'
import type { TradeLogItem } from '../../api/types'
import { fmtMoney, fmtNum, pnlColor } from '../../utils/format'

const TRADE_TYPES = ['开仓', '加仓', '减仓', '做T', '止损', '止盈', '清仓']

const TYPE_TAG_COLOR: Record<string, string> = {
  '开仓': 'red',
  '加仓': 'orange',
  '做T': 'purple',
  '止损': 'green',
  '止盈': 'cyan',
  '减仓': 'lime',
  '清仓': 'default'
}

/** 做T机制标签（T_REFACTOR：t_mode 字段，仅做T相关交易携带） */
const TMODE_LABEL: Record<string, string> = {
  grid: '网格',
  discipline: '纪律',
  time: '时点',
  off: '关'
}

export default function TradeLogTab({ trades }: { trades: TradeLogItem[] }) {
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [sideFilter, setSideFilter] = useState<string>('all')
  const [codeFilter, setCodeFilter] = useState<string>('all')

  // 交易涉及的全部股票（代码+名称，去重，按代码排序）
  const stockOptions = useMemo(() => {
    const m = new Map<string, string>()
    for (const t of trades) m.set(t.code, t.name || t.code)
    return [...m.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([code, name]) => ({ value: code, label: `${code} ${name}` }))
  }, [trades])

  const filtered = useMemo(
    () =>
      trades.filter(
        (t) =>
          (codeFilter === 'all' || t.code === codeFilter) &&
          (typeFilter === 'all' || t.type === typeFilter) &&
          (sideFilter === 'all' || t.side === sideFilter)
      ),
    [trades, codeFilter, typeFilter, sideFilter]
  )

  // 每笔交易成交后该股票的剩余持仓量（按交易时间顺序累计，过滤不影响数值）
  const remainByTrade = useMemo(() => {
    const map = new Map<number, number>()
    const cur = new Map<string, number>()
    for (const t of [...trades].sort((a, b) => a.trade_id - b.trade_id)) {
      const before = cur.get(t.code) ?? 0
      const after = t.side === 'buy' ? before + t.volume : before - t.volume
      map.set(t.trade_id, after)
      cur.set(t.code, after)
    }
    return map
  }, [trades])

  const exportCsv = () => {
    const headers = ['trade_id', '时间', '代码', '名称', '方向', '价格', '数量', '剩余持仓', '金额', '手续费', '类型', 'T模式', '理由', '平仓盈亏']
    const rows = filtered.map((t) => [
      t.trade_id,
      t.time,
      t.code,
      t.name,
      t.side,
      t.price,
      t.volume,
      remainByTrade.get(t.trade_id) ?? '',
      t.amount,
      t.fee,
      t.type,
      t.t_mode ?? '',
      t.reason ?? '',
      t.pnl ?? ''
    ])
    const escape = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv =
      '\uFEFF' + [headers.join(','), ...rows.map((r) => r.map(escape).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trade_log_${dayjs().format('YYYYMMDD_HHmmss')}.csv`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const columns: ColumnsType<TradeLogItem> = [
    { title: '#', dataIndex: 'trade_id', width: 56 },
    { title: '时间', dataIndex: 'time', width: 148 },
    { title: '代码', dataIndex: 'code', width: 84 },
    { title: '名称', dataIndex: 'name', width: 96, ellipsis: true },
    {
      title: '方向',
      dataIndex: 'side',
      width: 64,
      render: (v: 'buy' | 'sell') =>
        v === 'buy' ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>
    },
    {
      title: '段',
      dataIndex: 'seg',
      width: 52,
      render: (v: number | undefined) =>
        v != null ? <Tooltip title="动态选股段号（滚动重选）"><Tag color="geekblue">S{v}</Tag></Tooltip> : '-'
    },
    {
      title: '价格',
      dataIndex: 'price',
      width: 88,
      align: 'right',
      render: (v: number) => fmtNum(v, 3)
    },
    {
      title: '数量',
      dataIndex: 'volume',
      width: 92,
      align: 'right',
      render: (v: number) => v.toLocaleString('zh-CN')
    },
    {
      title: '剩余持仓',
      width: 92,
      align: 'right',
      render: (_v, t) => {
        const r = remainByTrade.get(t.trade_id)
        return r === undefined ? '-' : <span>{r.toLocaleString('zh-CN')}</span>
      }
    },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 112,
      align: 'right',
      render: (v: number) => fmtMoney(v)
    },
    {
      title: '手续费',
      dataIndex: 'fee',
      width: 88,
      align: 'right',
      render: (v: number) => fmtMoney(v)
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 110,
      render: (v: string, t) => (
        <Space size={4} wrap>
          <Tag color={TYPE_TAG_COLOR[v] ?? 'default'}>{v}</Tag>
          {t.t_mode && <Tag color="geekblue">{TMODE_LABEL[t.t_mode] ?? t.t_mode}</Tag>}
        </Space>
      )
    },
    {
      // 理由固定宽度两行展开：不设 width 的自适应列在窄屏会被固定列挤压成省略号，
      // 这是「理由老是被缩起来」的根因；2 行 clamp 保底，超长悬浮 Tooltip 看全文
      title: '理由',
      dataIndex: 'reason',
      width: 320,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v} placement="topLeft">
            <span
              style={{
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
                wordBreak: 'break-all',
                whiteSpace: 'normal'
              }}
            >
              {v}
            </span>
          </Tooltip>
        ) : (
          '-'
        )
    },
    {
      title: '平仓盈亏',
      dataIndex: 'pnl',
      width: 104,
      align: 'right',
      render: (v: number | null) =>
        v === null || v === undefined ? '-' : <span style={{ color: pnlColor(v) }}>{fmtMoney(v)}</span>
    }
  ]

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Text>股票：</Typography.Text>
        <Select
          value={codeFilter}
          onChange={setCodeFilter}
          style={{ width: 220 }}
          showSearch
          optionFilterProp="label"
          options={[{ value: 'all', label: '全部' }, ...stockOptions]}
        />
        <Typography.Text>类型：</Typography.Text>
        <Select
          value={typeFilter}
          onChange={setTypeFilter}
          style={{ width: 120 }}
          options={[
            { value: 'all', label: '全部' },
            ...TRADE_TYPES.map((t) => ({ value: t, label: t }))
          ]}
        />
        <Typography.Text>方向：</Typography.Text>
        <Select
          value={sideFilter}
          onChange={setSideFilter}
          style={{ width: 100 }}
          options={[
            { value: 'all', label: '全部' },
            { value: 'buy', label: '买入' },
            { value: 'sell', label: '卖出' }
          ]}
        />
        <Button icon={<DownloadOutlined />} onClick={exportCsv}>
          导出CSV
        </Button>
        <Typography.Text type="secondary">共 {filtered.length} 笔</Typography.Text>
      </Space>
      <Table<TradeLogItem>
        rowKey="trade_id"
        dataSource={filtered}
        columns={columns}
        size="small"
        scroll={{ x: 1506 }}
        pagination={{ pageSize: 50, showSizeChanger: true, showTotal: (t) => `共 ${t} 笔` }}
      />
    </div>
  )
}
