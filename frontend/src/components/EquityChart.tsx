import { useMemo } from 'react'
import { Empty } from 'antd'
import type { EquityPoint } from '../api/types'
import EchartsReact from './EchartsReact'

interface Props {
  data: EquityPoint[]
  /** 基准指数净值（与 data 同日期轴、归一化到初始资金；缺省不显示） */
  benchmark?: Array<{ date: string; equity: number }> | null
  /** 基准名称（图例/悬浮提示） */
  benchmarkName?: string
}

/** 资金曲线：上图 净值（面积）+ 基准指数（虚线）+ 仓位比例（右副轴，虚线），下图 回撤%（绿色面积向下），dataZoom 联动缩放 */
export default function EquityChart({ data, benchmark, benchmarkName }: Props) {
  const option = useMemo(() => {
    const dates = data.map((d) => d.date)
    const equities = data.map((d) => d.equity)
    const drawdowns = data.map((d) => +(d.drawdown * 100).toFixed(3))
    const hasPos = data.some((d) => d.position_ratio !== undefined && d.position_ratio !== null)
    const posRatios = data.map((d) =>
      d.position_ratio === undefined || d.position_ratio === null ? null : +(d.position_ratio * 100).toFixed(1)
    )
    const benchName = benchmarkName || '基准指数'
    const benchData = benchmark && benchmark.length === data.length
      ? benchmark.map((b) => b.equity)
      : null
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      legend: {
        data: [
          '账户权益',
          ...(benchData ? [benchName] : []),
          ...(hasPos ? ['仓位比例'] : []),
          '回撤%'
        ]
      },
      grid: [
        { left: 80, right: 70, top: 40, height: '46%' },
        { left: 80, right: 70, top: '66%', height: '20%' }
      ],
      xAxis: [
        { type: 'category', gridIndex: 0, data: dates },
        { type: 'category', gridIndex: 1, data: dates, axisLabel: { show: false }, axisTick: { show: false } }
      ],
      yAxis: [
        {
          type: 'value',
          gridIndex: 0,
          scale: true,
          axisLabel: { formatter: (v: number) => `${(v / 10000).toFixed(0)}万` }
        },
        {
          type: 'value',
          gridIndex: 0,
          position: 'right',
          min: 0,
          max: 100,
          axisLabel: { formatter: '{value}%' },
          splitLine: { show: false }
        },
        {
          type: 'value',
          gridIndex: 1,
          inverse: true,
          axisLabel: { formatter: (v: number) => `${v}%` }
        }
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 8, height: 18 }
      ],
      series: [
        {
          name: '账户权益',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: equities,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1f4e79' },
          itemStyle: { color: '#1f4e79' },
          areaStyle: { color: 'rgba(31,78,121,0.12)' }
        },
        ...(benchData
          ? [
              {
                name: benchName,
                type: 'line' as const,
                xAxisIndex: 0,
                yAxisIndex: 0,
                data: benchData,
                showSymbol: false,
                connectNulls: true,
                lineStyle: { type: 'dashed' as const, width: 1.5, color: '#8c8c8c' },
                itemStyle: { color: '#8c8c8c' }
              }
            ]
          : []),
        ...(hasPos
          ? [
              {
                name: '仓位比例',
                type: 'line' as const,
                xAxisIndex: 0,
                yAxisIndex: 1,
                data: posRatios,
                showSymbol: false,
                connectNulls: true,
                lineStyle: { type: 'dashed' as const, width: 1, opacity: 0.75 },
                itemStyle: { color: '#fa8c16' }
              }
            ]
          : []),
        {
          name: '回撤%',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: drawdowns,
          showSymbol: false,
          itemStyle: { color: '#52c41a' },
          lineStyle: { color: '#52c41a', width: 1 },
          areaStyle: { color: 'rgba(82,196,26,0.25)' }
        }
      ]
    }
  }, [data, benchmark, benchmarkName])

  if (!data || data.length === 0) {
    return <Empty description="暂无资金曲线数据" />
  }
  return <EchartsReact option={option} height={520} />
}
