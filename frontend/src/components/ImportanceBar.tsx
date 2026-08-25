import { useMemo } from 'react'
import { Empty } from 'antd'
import EchartsReact from './EchartsReact'

/** 参数重要性横向条形图 */
export default function ImportanceBar({ data }: { data: Record<string, number> }) {
  const entries = useMemo(
    () =>
      Object.entries(data ?? {})
        .filter(([, v]) => Number.isFinite(v))
        .sort((a, b) => a[1] - b[1]),
    [data]
  )

  const option = useMemo(
    () => ({
      tooltip: { trigger: 'axis' },
      grid: { left: 140, right: 60, top: 10, bottom: 24 },
      xAxis: { type: 'value', max: 1 },
      yAxis: { type: 'category', data: entries.map((e) => e[0]) },
      series: [
        {
          type: 'bar',
          data: entries.map((e) => +e[1].toFixed(4)),
          barMaxWidth: 22,
          itemStyle: { color: '#1f4e79' },
          label: { show: true, position: 'right', formatter: (p: { value: number }) => p.value.toFixed(3) }
        }
      ]
    }),
    [entries]
  )

  if (entries.length === 0) {
    return <Empty description="暂无参数重要性数据" />
  }
  return <EchartsReact option={option} height={Math.max(160, entries.length * 42 + 80)} />
}
