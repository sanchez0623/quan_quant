import { useEffect, useRef } from 'react'
import type { CSSProperties } from 'react'
import * as echarts from 'echarts'

type EchartsInstance = ReturnType<typeof echarts.init>

interface Props {
  option: echarts.EChartsCoreOption
  height?: number
  style?: CSSProperties
}

/** ECharts 通用容器：自动 init / resize / dispose */
export default function EchartsReact({ option, height = 320, style }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<EchartsInstance | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = echarts.init(el)
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={containerRef} style={{ width: '100%', height, ...style }} />
}
