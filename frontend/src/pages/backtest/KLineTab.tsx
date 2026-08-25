import { useEffect, useMemo, useState } from 'react'
import { Empty, message, Select, Space, Spin, Typography } from 'antd'
import { errDetail, getKline } from '../../api/client'
import type { KLineResponse, TradeLogItem } from '../../api/types'
import KLineChart from '../../components/KLineChart'

interface Props {
  taskId: string
  universe: string[]
  trades: TradeLogItem[]
}

export default function KLineTab({ taskId, universe, trades }: Props) {
  const [code, setCode] = useState<string>(universe[0] ?? '')
  const [data, setData] = useState<KLineResponse | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!code) return
    let cancelled = false
    setLoading(true)
    getKline(taskId, code)
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch((err) => {
        if (!cancelled) {
          message.error(errDetail(err, '加载K线失败'))
          setData(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId, code])

  // 按 trade_id 关联交易理由
  const reasonMap = useMemo(() => {
    const mp = new Map<number, string | null>()
    trades.forEach((t) => mp.set(t.trade_id, t.reason ?? null))
    return mp
  }, [trades])

  const marksWithReason = useMemo(
    () => (data?.marks ?? []).map((m) => ({ ...m, reason: reasonMap.get(m.trade_id) ?? null })),
    [data, reasonMap]
  )

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Text>股票：</Typography.Text>
        <Select
          value={code || undefined}
          onChange={setCode}
          style={{ width: 220 }}
          placeholder="选择股票"
          showSearch
          optionFilterProp="label"
          options={universe.map((c) => ({ value: c, label: c }))}
        />
        {data && <Typography.Text type="secondary">{data.name}</Typography.Text>}
      </Space>
      <Spin spinning={loading}>
        {data && data.bars.length > 0 ? (
          <KLineChart bars={data.bars} marks={marksWithReason} height={480} />
        ) : (
          <Empty description="暂无K线数据" />
        )}
      </Spin>
    </div>
  )
}
