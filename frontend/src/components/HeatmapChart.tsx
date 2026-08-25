import { useMemo } from 'react'
import { Empty } from 'antd'
import type { MonthlyReturn } from '../api/types'
import EchartsReact from './EchartsReact'

const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']

/** 月度收益热力图：x=月份 1-12，y=年份，色带红涨绿跌 */
export default function HeatmapChart({ data }: { data: MonthlyReturn[] }) {
  const option = useMemo(() => {
    const years = Array.from(new Set(data.map((d) => d.year))).sort((a, b) => a - b)
    const maxAbs = Math.max(0.01, ...data.map((d) => Math.abs(d.return))) * 100
    const cells = data.map((d) => [d.month - 1, years.indexOf(d.year), +(d.return * 100).toFixed(2)])
    return {
      tooltip: {
        position: 'top',
        formatter: (p: { value: [number, number, number] }) =>
          `${years[p.value[1]]}年${p.value[0] + 1}月：${p.value[2].toFixed(2)}%`
      },
      grid: { left: 60, right: 30, top: 10, bottom: 80 },
      xAxis: { type: 'category', data: MONTH_LABELS, splitArea: { show: true } },
      yAxis: { type: 'category', data: years.map(String), splitArea: { show: true } },
      visualMap: {
        min: -maxAbs,
        max: maxAbs,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        inRange: { color: ['#52c41a', '#ffffff', '#f5222d'] },
        formatter: (v: number) => `${v.toFixed(1)}%`
      },
      series: [
        {
          type: 'heatmap',
          data: cells,
          label: {
            show: true,
            fontSize: 10,
            formatter: (p: { data: [number, number, number] }) => p.data[2].toFixed(1)
          },
          emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.3)' } }
        }
      ]
    }
  }, [data])

  if (!data || data.length === 0) {
    return <Empty description="暂无月度收益数据" />
  }
  return <EchartsReact option={option} height={340} />
}
