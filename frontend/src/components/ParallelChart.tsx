import { useMemo } from 'react'
import { Empty } from 'antd'
import type { TrialItem } from '../api/types'
import EchartsReact from './EchartsReact'

interface Props {
  trials: TrialItem[]
  metric: string
}

/** 寻优 trials 平行坐标图：各参数维度 + 目标值维度，最优 trial 红色高亮 */
export default function ParallelChart({ trials, metric }: Props) {
  const option = useMemo(() => {
    const valid = trials.filter(
      (t) => t.state === 'complete' && t.value !== null && t.value !== undefined
    )
    const keys = Array.from(new Set(valid.flatMap((t) => Object.keys(t.params ?? {}))))
    if (valid.length === 0 || keys.length === 0) return null

    type AxisInfo = { dim: number; name: string; type: 'value' | 'category'; min?: number; max?: number; data?: string[] }
    const axisInfo: AxisInfo[] = []
    const dimData: number[][] = []

    keys.forEach((k, idx) => {
      const raw = valid.map((t) => t.params[k])
      const allNum = raw.every((v) => typeof v === 'number')
      if (allNum) {
        const nums = raw as number[]
        const min = Math.min(...nums)
        const max = Math.max(...nums)
        dimData.push(nums)
        axisInfo.push({
          dim: idx,
          name: k,
          type: 'value',
          min: min === max ? min - 1 : min,
          max: min === max ? max + 1 : max
        })
      } else {
        const cats = Array.from(new Set(raw.map(String)))
        const catIndex = new Map(cats.map((c, i) => [c, i]))
        dimData.push(raw.map((v) => catIndex.get(String(v)) ?? 0))
        axisInfo.push({ dim: idx, name: k, type: 'category', data: cats })
      }
    })

    const vals = valid.map((t) => t.value)
    const vmin = Math.min(...vals)
    const vmax = Math.max(...vals)
    axisInfo.push({
      dim: keys.length,
      name: metric,
      type: 'value',
      min: vmin === vmax ? vmin - 1 : vmin,
      max: vmin === vmax ? vmax + 1 : vmax
    })

    const lines = valid.map((t, i) => ({
      value: [...dimData.map((d) => d[i]), t.value],
      lineStyle: {
        width: t.value === vmax ? 2.5 : 1,
        opacity: t.value === vmax ? 0.95 : 0.4,
        color: t.value === vmax ? '#f5222d' : '#5b8ff9'
      }
    }))

    return {
      parallelAxis: axisInfo,
      parallel: { left: 90, right: 90, top: 50, bottom: 30 },
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'parallel',
          smooth: true,
          lineStyle: { width: 1, opacity: 0.4 },
          data: lines
        }
      ]
    }
  }, [trials, metric])

  if (!option) {
    return <Empty description="暂无试验数据" />
  }
  return <EchartsReact option={option} height={360} />
}
