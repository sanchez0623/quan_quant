import { useMemo } from 'react'
import { Empty } from 'antd'
import type { MonthlyReturn } from '../api/types'
import EchartsReact from './EchartsReact'

const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']

/** 月度收益热力图：x=月份 1-12，y=年份。
 * 每格高度与收益率成比例（正收益向上、负收益向下，中线为 0），色带红涨绿跌。
 */
export default function HeatmapChart({ data }: { data: MonthlyReturn[] }) {
  const option = useMemo(() => {
    const years = Array.from(new Set(data.map((d) => d.year))).sort((a, b) => a - b)
    const maxAbs = Math.max(0.01, ...data.map((d) => Math.abs(d.return))) * 100
    // [月份索引, 年份索引, 收益率%]
    const cells = data.map((d) => [d.month - 1, years.indexOf(d.year), +(d.return * 100).toFixed(2)])
    const halfCellH = (nYears: number) => 220 / nYears / 2

    return {
      tooltip: {
        position: 'top',
        formatter: (p: { value?: [number, number, number] }) =>
          p.value ? `${years[p.value[1]]}年${p.value[0] + 1}月：${p.value[2].toFixed(2)}%` : ''
      },
      grid: { left: 60, right: 30, top: 20, bottom: 40 },
      xAxis: { type: 'category', data: MONTH_LABELS, splitArea: { show: true } },
      yAxis: { type: 'category', data: years.map(String), splitArea: { show: true } },
      series: [
        {
          type: 'custom',
          data: cells,
          renderItem: (params: unknown, api: any) => {
            const mi = api.value(0) as number
            const yi = api.value(1) as number
            const pct = (api.value(2) as number) ?? 0
            const x = api.coord([mi, yi])[0]
            const cy = api.coord([mi, yi])[1]
            const halfW = api.size([1, 0])[0] * 0.36
            const maxH = halfCellH(years.length) * 0.85
            const h = Math.max((Math.abs(pct) / maxAbs) * maxH, 1)
            const up = pct >= 0
            const y = up ? cy - h : cy
            return {
              type: 'group',
              children: [
                {
                  type: 'rect',
                  shape: { x: x - halfW, y, width: halfW * 2, height: h },
                  style: api.style({
                    fill: up ? '#f5222d' : '#52c41a',
                    opacity: 0.85
                  })
                },
                {
                  type: 'text',
                  style: {
                    text: pct.toFixed(1),
                    x,
                    y: up ? y - 3 : y + h + 10,
                    textAlign: 'center',
                    verticalAlign: 'middle',
                    fill: '#666',
                    fontSize: 10
                  }
                }
              ]
            }
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
