import { useEffect, useMemo, useState } from 'react'
import { Empty, message, Select, Space, Spin, Typography } from 'antd'
import { errDetail, getKline } from '../../api/client'
import type { KLineResponse, Period, TradeLogItem } from '../../api/types'
import KLineChart from '../../components/KLineChart'

interface Props {
  taskId: string
  universe: string[]
  trades: TradeLogItem[]
  reportPeriod?: string
}

const PERIOD_OPTIONS: Array<{ value: Period; label: string }> = [
  { value: 'daily', label: '日线' },
  { value: 'minute5', label: '5分钟' }
]

export default function KLineTab({ taskId, universe, trades, reportPeriod }: Props) {
  // 股票候选：静态池用 config.universe；动态选股(universe_auto)池为空 ->
  // 回退用实际交易过的股票（trades 已含 code），否则下拉无选项、K线无法展示
  const stockOptions = useMemo(() => {
    const fromUniverse = (universe ?? []).filter(Boolean)
    if (fromUniverse.length > 0) return fromUniverse
    return Array.from(new Set((trades ?? []).map((t) => t.code).filter(Boolean)))
  }, [universe, trades])

  const [code, setCode] = useState<string>(stockOptions[0] ?? '')
  // 图表周期默认日线（交易点更易观察；5分钟回测也能切回分钟级看细节）
  const [period, setPeriod] = useState<Period>('daily')
  const [data, setData] = useState<KLineResponse | null>(null)
  const [loading, setLoading] = useState(false)

  // 候选列表就绪后确保当前选中的 code 有效
  useEffect(() => {
    if (stockOptions.length > 0 && !stockOptions.includes(code)) {
      setCode(stockOptions[0])
    }
  }, [code, stockOptions])

  useEffect(() => {
    if (!code) return
    let cancelled = false
    setLoading(true)
    getKline(taskId, code, period)
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
  }, [taskId, code, period])

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
          style={{ width: 150 }}
          placeholder="选择股票"
          showSearch
          optionFilterProp="label"
          options={stockOptions.map((c) => ({ value: c, label: c }))}
        />
        <Typography.Text>周期：</Typography.Text>
        <Select<Period>
          value={period}
          onChange={setPeriod}
          style={{ width: 110 }}
          options={PERIOD_OPTIONS}
        />
        {data && <Typography.Text type="secondary">{data.name}</Typography.Text>}
        {reportPeriod === 'minute5' && period === 'daily' && (
          <Typography.Text type="secondary">（回测周期为5分钟，当前按日线展示交易点）</Typography.Text>
        )}
      </Space>
      <Spin spinning={loading}>
        {data && data.bars.length > 0 ? (
          <KLineChart bars={data.bars} marks={marksWithReason} height={480} />
        ) : (
          <Empty description="暂无该周期K线数据" />
        )}
      </Spin>
    </div>
  )
}
