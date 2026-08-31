import { useState } from 'react'
import { Card, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { AutoSegmentInfo, MomentumPickItem } from '../../api/types'

const pickColumns: ColumnsType<MomentumPickItem> = [
  { title: '#', dataIndex: 'rank', width: 48 },
  { title: '代码', dataIndex: 'code', width: 84 },
  { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
  {
    title: '动量分', dataIndex: 'score', width: 90, align: 'right',
    render: (v: number) => (typeof v === 'number' ? v.toFixed(4) : '-')
  },
  {
    title: 'RPS', dataIndex: 'rps', width: 72, align: 'right',
    render: (v: number | null) => (v != null ? v.toFixed(1) : '-')
  }
]

export default function AutoSegmentsTab({ segments }: { segments: AutoSegmentInfo[] }) {
  const [active, setActive] = useState<string>(String(segments[0]?.seg ?? 1))
  const cur = segments.find((s) => String(s.seg) === active) ?? segments[0]
  const cols: ColumnsType<AutoSegmentInfo> = [
    {
      title: '段', dataIndex: 'seg', width: 64,
      render: (v: number, r) => (
        <Tag color={r.trigger_day ? 'geekblue' : 'green'}>S{v}</Tag>
      )
    },
    { title: '起', dataIndex: 'start', width: 106 },
    { title: '止', dataIndex: 'end', width: 106 },
    { title: '基准日(T-1)', dataIndex: 'as_of', width: 110 },
    { title: '池子', width: 76, render: (_v, r) => `${r.universe?.length ?? 0} 只` },
    {
      title: '重选触发', dataIndex: 'trigger_day', width: 190,
      render: (v: string | undefined, r) =>
        v ? `${v}（${r.trigger_reason ?? ''}）` : <Typography.Text type="secondary">回测结束</Typography.Text>
    }
  ]
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        动态选股（universe_auto）：每段池子以段首前一交易日收盘的动量趋势预筛生成（门槛+RPS+排序取前x，
        无后视镜）；全空仓持续 N 个交易日后自动重选换池，旧池退役；全市场无票过门槛时保持空仓不硬买。
      </Typography.Text>
      <Table
        size="small"
        rowKey="seg"
        dataSource={segments}
        columns={cols}
        pagination={false}
        rowClassName={(r) => (String(r.seg) === active ? 'ant-table-row-selected' : '')}
        onRow={(r) => ({ onClick: () => setActive(String(r.seg)), style: { cursor: 'pointer' } })}
      />
      {cur && (
        <Card size="small" title={`S${cur.seg} 池子明细（基准日 ${cur.as_of}，${cur.picked?.length ?? 0} 只）`}>
          <Table
            size="small"
            rowKey="code"
            dataSource={cur.picked ?? []}
            columns={pickColumns}
            pagination={{ pageSize: 10, size: 'small' }}
          />
          {cur.trigger_day && cur.next_picked && cur.next_picked.length > 0 && (
            <Card
              size="small"
              type="inner"
              style={{ marginTop: 8 }}
              title={`S${cur.seg + 1} 下一池预览（基准日 ${cur.trigger_day}，${cur.next_picked.length} 只）`}
            >
              <Table
                size="small"
                rowKey="code"
                dataSource={cur.next_picked}
                columns={pickColumns}
                pagination={{ pageSize: 10, size: 'small' }}
              />
            </Card>
          )}
          {cur.trigger_day && (!cur.next_picked || cur.next_picked.length === 0) && (
            <Typography.Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
              触发日全市场无票过门槛：转入空仓等待，直到市场重新出现符合条件者再开新段。
            </Typography.Text>
          )}
        </Card>
      )}
    </Space>
  )
}
