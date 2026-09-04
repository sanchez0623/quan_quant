import { useState } from 'react'
import { Button, message } from 'antd'
import { StopOutlined } from '@ant-design/icons'
import { cancelTask, errDetail } from '../api/client'

interface Props {
  taskId: string
  /** 取消请求成功后的回调（可选） */
  onStopped?: () => void
  /** 按钮文案，默认「停止」 */
  label?: string
}

/** 任务协作式取消按钮：请求后子进程在下一进度检查点退出（不在写库中途强杀）。
 * 适用于任务列表行内或任务进度条旁（running/pending 状态展示）。 */
export default function TaskStopButton({ taskId, onStopped, label = '停止' }: Props) {
  const [loading, setLoading] = useState(false)
  const onClick = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setLoading(true)
    try {
      await cancelTask(taskId)
      message.info('已请求停止，任务将在当前检查点退出')
      onStopped?.()
    } catch (err) {
      message.error(errDetail(err, '请求停止失败'))
    } finally {
      setLoading(false)
    }
  }
  return (
    <Button size="small" danger type="text" icon={<StopOutlined />}
            loading={loading} onClick={onClick}>
      {label}
    </Button>
  )
}
