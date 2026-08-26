import { useEffect, useRef, useState } from 'react'
import { getTaskStatus } from '../api/client'
import type { TaskStatus } from '../api/types'

export interface TaskProgress {
  status: TaskStatus | null
  progress: number
  message: string
}

interface WsPayload {
  status?: TaskStatus
  progress?: number
  message?: string | null
  error?: string | null
}

const FINAL_STATES: TaskStatus[] = ['success', 'failed', 'cancelled']

/**
 * 任务进度 hook：优先 WebSocket /ws/tasks/{id}，失败时回退为每 1s 轮询
 * GET /api/backtests/{id}/status。任务进入终态时回调 onDone(status, fullState)。
 */
export function useTaskProgress(
  taskId: string | null | undefined,
  onDone?: (status: TaskStatus, fullState: TaskProgress | null) => void
): TaskProgress {
  const [state, setState] = useState<TaskProgress>({ status: null, progress: 0, message: '' })
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  useEffect(() => {
    if (!taskId) {
      setState({ status: null, progress: 0, message: '' })
      return
    }

    let finished = false
    let ws: WebSocket | null = null
    let pollTimer: number | null = null
    let pollStarted = false

    const cleanup = () => {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer)
        pollTimer = null
      }
      if (ws) {
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        try {
          ws.close()
        } catch {
          /* ignore */
        }
        ws = null
      }
    }

    const handle = (data: WsPayload) => {
      if (finished) return
      if (data.status !== undefined || data.progress !== undefined || data.message || data.error) {
        setState((prev) => ({
          status: data.status ?? prev.status,
          progress: typeof data.progress === 'number' ? data.progress : prev.progress,
          message: data.message || data.error || prev.message
        }))
      }
      if (data.status !== undefined && FINAL_STATES.includes(data.status)) {
        finished = true
        if (data.status === 'success') {
          setState((prev) => ({ ...prev, progress: 100 }))
        }
        // 传递完整状态给回调，包含错误信息
        onDoneRef.current?.(data.status, {
          status: data.status,
          progress: 100,
          message: data.message || data.error || ''
        })
        cleanup()
      }
    }

    const startPolling = () => {
      if (pollStarted || finished) return
      pollStarted = true
      const poll = async () => {
        if (finished) return
        try {
          const res = await getTaskStatus(taskId)
          handle(res)
        } catch {
          /* 进度接口异常（如 404）忽略，下次继续 */
        }
        if (!finished) {
          pollTimer = window.setTimeout(poll, 1000)
        }
      }
      poll()
    }

    try {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${window.location.host}/ws/tasks/${taskId}`)
      ws.onmessage = (ev: MessageEvent) => {
        try {
          handle(JSON.parse(ev.data as string))
        } catch {
          /* ignore */
        }
      }
      ws.onerror = () => {
        ws?.close()
      }
      ws.onclose = () => startPolling()
    } catch {
      startPolling()
    }

    return () => {
      finished = true
      cleanup()
    }
  }, [taskId])

  return state
}
