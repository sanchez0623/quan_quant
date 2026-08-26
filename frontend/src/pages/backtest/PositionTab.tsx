import { Empty, Table } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { PositionSnapshot, PositionSnapshotPosition } from '../../api/types'
import { fmtInt, fmtMoney, fmtNum } from '../../utils/format'

const posColumns: ColumnsType<PositionSnapshotPosition> = [
  { title: '代码', dataIndex: 'code', width: 110 },
  { title: '名称', dataIndex: 'name', width: 120, render: (v?: string) => v || '-' },
  {
    title: '持仓数量',
    dataIndex: 'volume',
    align: 'right',
    render: (v: number) => fmtInt(v)
  },
  {
    title: '成本价',
    dataIndex: 'cost',
    align: 'right',
    render: (v: number) => fmtNum(v, 3)
  },
  {
    title: '成本市值',
    key: 'cost_value',
    align: 'right',
    render: (_, r) => fmtMoney(r.volume * r.cost)
  }
]

export default function PositionTab({ snapshots }: { snapshots: PositionSnapshot[] }) {
  const columns: ColumnsType<PositionSnapshot> = [
    { title: '日期', dataIndex: 'date', width: 120 },
    {
      title: '现金',
      dataIndex: 'cash',
      align: 'right',
      render: (v: number) => fmtMoney(v)
    },
    {
      title: '持仓市值',
      dataIndex: 'market_value',
      align: 'right',
      render: (v: number) => fmtMoney(v)
    },
    {
      title: '总权益',
      key: 'total',
      align: 'right',
      render: (_, r) => fmtMoney(r.cash + r.market_value)
    },
    {
      title: '持仓只数',
      key: 'count',
      width: 100,
      align: 'right',
      render: (_, r) => `${r.positions?.length ?? 0}`
    }
  ]

  return (
    <Table<PositionSnapshot>
      rowKey="date"
      dataSource={snapshots}
      columns={columns}
      size="small"
      pagination={{ pageSize: 30, showTotal: (t) => `共 ${t} 天` }}
      expandable={{
        expandedRowRender: (r) =>
          r.positions && r.positions.length > 0 ? (
            <Table<PositionSnapshotPosition>
              rowKey="code"
              dataSource={r.positions}
              columns={posColumns}
              size="small"
              pagination={false}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当日无持仓" />
          )
      }}
    />
  )
}
