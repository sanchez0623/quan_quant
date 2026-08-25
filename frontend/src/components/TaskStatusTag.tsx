import { Tag } from 'antd'
import type { TaskStatus } from '../api/types'

const COLOR_MAP: Record<TaskStatus, string> = {
  pending: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
  cancelled: 'warning'
}

const TEXT_MAP: Record<TaskStatus, string> = {
  pending: '等待中',
  running: '运行中',
  success: '成功',
  failed: '失败',
  cancelled: '已取消'
}

export default function TaskStatusTag({ status }: { status: TaskStatus }) {
  return <Tag color={COLOR_MAP[status] ?? 'default'}>{TEXT_MAP[status] ?? status}</Tag>
}
