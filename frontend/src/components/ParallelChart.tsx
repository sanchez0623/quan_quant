import { useMemo } from 'react'
import { Card, Empty } from 'antd'
import type { TrialItem } from '../api/types'
import EchartsReact from './EchartsReact'

interface Props {
  trials: TrialItem[]
  metric: string
}

type ParallelOption = {
  parallelAxis: Array<{ dim: number; name: string; type: 'value' | 'category'; min?: number; max?: number; data?: string[] }>
  parallel: { left: number; right: number; top: number; bottom: number }
  tooltip: { trigger: string }
  series: Array<Record<string, unknown>>
}

function buildOption(trials: TrialItem[], metric: string): ParallelOption | null {
  const valid = trials.filter(
    (t) => t.state === 'complete' && t.value !== null && t.value !== undefined
  )
  if (valid.length === 0) return null
  // 只保留"该组所有 trial 都具备"的参数：分组寻优下其它组缺失的参数
  // 会导致 undefined 出现在轴与连线里
  const allKeys = Array.from(new Set(valid.flatMap((t) => Object.keys(t.params ?? {}))))
  const keys = allKeys.filter((k) => valid.every((t) => k in (t.params ?? {})))
  if (keys.length === 0) return null

  type AxisInfo = {
    dim: number
    name: string
    type: 'value' | 'category'
    min?: number
    max?: number
    data?: string[]
  }
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
      const cats = Array.from(
        new Set(raw.filter((v) => v !== undefined && v !== null).map(String))
      )
      const catIndex = new Map(cats.map((c, i) => [c, i]))
      dimData.push(raw.map((v) => (v === undefined || v === null ? 0 : catIndex.get(String(v)) ?? 0)))
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
    series: [{ type: 'parallel', smooth: true, lineStyle: { width: 1, opacity: 0.4 }, data: lines }]
  }
}

/** 寻优 trials 平行坐标图：分组寻优按组拆分（每组一套参数轴），最优 trial 红色高亮 */
export default function ParallelChart({ trials, metric }: Props) {
  const charts = useMemo(() => {
    const byGroup = new Map<string, TrialItem[]>()
    for (const t of trials) {
      const g = t.group && t.group.trim() ? t.group : '__all__'
      if (!byGroup.has(g)) byGroup.set(g, [])
      byGroup.get(g)!.push(t)
    }
    const out: Array<{ name: string | null; option: ParallelOption }> = []
    for (const [g, ts] of byGroup) {
      const opt = buildOption(ts, metric)
      if (opt) out.push({ name: g === '__all__' ? null : g, option: opt })
    }
    return out
  }, [trials, metric])

  if (charts.length === 0) {
    return <Empty description="暂无试验数据" />
  }
  // 平铺模式（单一参数集）：保持原来的单图布局
  if (charts.length === 1 && charts[0].name === null) {
    return <EchartsReact option={charts[0].option} height={360} />
  }
  // 分组模式：每个参数组一张图
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {charts.map((c) => (
        <Card key={c.name ?? 'group'} size="small" title={`参数组：${c.name}`}>
          <EchartsReact option={c.option} height={300} />
        </Card>
      ))}
    </div>
  )
}
