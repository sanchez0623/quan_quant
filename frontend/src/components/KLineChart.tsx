import { useEffect, useRef, useState } from 'react'
import { Modal } from 'antd'
import { ActionType, dispose, init, registerIndicator } from 'klinecharts'
import type { IndicatorDrawParams, IndicatorTemplate } from 'klinecharts'
import type { KLineBar, KLineMark } from '../api/types'

type ChartInstance = ReturnType<typeof init>

/** 交易标记颜色：开仓红 / 加仓橙 / 做T紫 / 止损深绿，其余绿 */
const MARK_COLORS: Record<string, string> = {
  '开仓': '#f5222d',
  '加仓': '#fa8c16',
  '做T': '#722ed1',
  '止损': '#389e0d',
  '止盈': '#13c2c2',
  '减仓': '#52c41a',
  '清仓': '#52c41a'
}

const TYPE_SHORT: Record<string, string> = {
  '开仓': '开',
  '加仓': '加',
  '做T': 'T',
  '止损': '损',
  '止盈': '盈',
  '减仓': '减',
  '清仓': '清'
}

interface TradeMarkPoint {
  dataIndex: number
  price: number
  side: string
  type: string
  time: string
  reason?: string | null
}

function parseTs(dateStr: string): number {
  return new Date(dateStr.replace(' ', 'T')).getTime()
}

let indicatorRegistered = false

function ensureIndicatorRegistered(): void {
  if (indicatorRegistered) return
  indicatorRegistered = true

  const template: IndicatorTemplate<TradeMarkPoint> = {
    name: 'TRADE_MARKS',
    shortName: '交易标记',
    calc: () => [],
    // 交易标记用自定义指标绘制：买入▲在K线下方、卖出▼在K线上方，按类型着色
    draw: ({ ctx, indicator, visibleRange, xAxis, yAxis }: IndicatorDrawParams<TradeMarkPoint>) => {
      const marks = (indicator.extendData ?? []) as TradeMarkPoint[]
      if (!marks || marks.length === 0) return false
      for (const m of marks) {
        if (m.dataIndex < visibleRange.from - 2 || m.dataIndex > visibleRange.to + 2) continue
        const x = xAxis.convertToPixel(m.dataIndex)
        const y = yAxis.convertToPixel(m.price)
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue
        const isBuy = m.side === 'buy'
        const color = MARK_COLORS[m.type] ?? (isBuy ? '#f5222d' : '#52c41a')
        ctx.fillStyle = color
        ctx.beginPath()
        if (isBuy) {
          const top = y + 6
          ctx.moveTo(x, top)
          ctx.lineTo(x - 5, top + 9)
          ctx.lineTo(x + 5, top + 9)
        } else {
          const bottom = y - 6
          ctx.moveTo(x, bottom)
          ctx.lineTo(x - 5, bottom - 9)
          ctx.lineTo(x + 5, bottom - 9)
        }
        ctx.closePath()
        ctx.fill()
        ctx.font = '10px sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'alphabetic'
        ctx.fillText(TYPE_SHORT[m.type] ?? m.type, x, isBuy ? y + 28 : y - 18)
      }
      return false
    }
  }
  registerIndicator(template)
}

interface Props {
  bars: KLineBar[]
  marks: KLineMark[]
  height?: number
}

export default function KLineChart({ bars, marks, height = 480 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ChartInstance>(null)
  const markPointsRef = useRef<TradeMarkPoint[]>([])
  const lastDataIndexRef = useRef<number | null>(null)
  const [hoverInfo, setHoverInfo] = useState<string | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    ensureIndicatorRegistered()
    const chart = init(el)
    if (!chart) return
    chartRef.current = chart

    try {
      chart.createIndicator('VOL', false, { id: 'pane_vol' })
    } catch {
      /* ignore */
    }

    try {
      chart.subscribeAction(ActionType.OnCrosshairChange, (params: unknown) => {
        const p = params as { data?: { dataIndex?: number }; dataIndex?: number }
        const di = p?.data?.dataIndex ?? p?.dataIndex ?? null
        lastDataIndexRef.current = di
        const m = di === null ? undefined : markPointsRef.current.find((x) => x.dataIndex === di)
        setHoverInfo(m ? `${m.time} ${m.side === 'buy' ? '买入' : '卖出'} · ${m.type} @ ${m.price}` : null)
      })
    } catch {
      /* ignore */
    }

    const ro = new ResizeObserver(() => {
      try {
        chart.resize()
      } catch {
        /* ignore */
      }
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      try {
        dispose(chart)
      } catch {
        /* ignore */
      }
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dataList = bars.map((b) => ({
      timestamp: parseTs(b.date),
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
      turnover: b.close * b.volume
    }))
    chart.applyNewData(dataList)

    const tsIndex = new Map<number, number>()
    dataList.forEach((d, i) => tsIndex.set(d.timestamp, i))
    const points: TradeMarkPoint[] = []
    for (const m of marks) {
      const di = tsIndex.get(parseTs(m.time))
      if (di === undefined) continue
      points.push({ dataIndex: di, price: m.price, side: m.side, type: m.type, time: m.time, reason: m.reason ?? null })
    }
    markPointsRef.current = points
    try {
      chart.createIndicator(
        { name: 'TRADE_MARKS', extendData: points },
        false,
        { id: 'candle_pane' }
      )
    } catch {
      /* ignore */
    }
  }, [bars, marks])

  const onChartClick = () => {
    const di = lastDataIndexRef.current
    if (di === null) return
    const m = markPointsRef.current.find((x) => x.dataIndex === di)
    if (!m) return
    Modal.info({
      title: `${m.side === 'buy' ? '买入' : '卖出'} · ${m.type}`,
      content: (
        <div>
          <p>时间：{m.time}</p>
          <p>价格：{m.price}</p>
          {m.reason ? <p>理由：{m.reason}</p> : null}
        </div>
      )
    })
  }

  return (
    <div>
      <div ref={containerRef} style={{ width: '100%', height }} onClick={onChartClick} />
      <div
        style={{
          minHeight: 22,
          marginTop: 4,
          fontSize: 12,
          color: hoverInfo ? '#1f4e79' : '#999'
        }}
      >
        {hoverInfo
          ? `交易标记：${hoverInfo}（点击查看详情）`
          : '提示：▲买入标记在K线下方、▼卖出标记在上方，按类型着色（开仓红/加仓橙/做T紫/止损深绿）；悬停或点击标记查看详情'}
      </div>
    </div>
  )
}
