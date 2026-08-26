import { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Alert, Button, Card, message, Progress, Space, Spin, Tabs, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { errDetail, getBacktestReport, getTaskStatus } from '../api/client'
import type { BacktestReport, TaskStatus, TaskStatusResponse } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import TaskStatusTag from '../components/TaskStatusTag'
import KLineTab from './backtest/KLineTab'
import MetricsTab from './backtest/MetricsTab'
import TradeLogTab from './backtest/TradeLogTab'
import PositionTab from './backtest/PositionTab'
import EquityChart from '../components/EquityChart'

export default function BacktestResult() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [statusResp, setStatusResp] = useState<TaskStatusResponse | null>(null)
  const [report, setReport] = useState<BacktestReport | null>(null)
  const [reportLoading, setReportLoading] = useState(false)

  const loadReport = useCallback(async () => {
    if (!id) return
    setReportLoading(true)
    try {
      setReport(await getBacktestReport(id))
    } catch (err) {
      message.error(errDetail(err, '加载报告失败'))
    } finally {
      setReportLoading(false)
    }
  }, [id])

  useEffect(() => {
    if (!id) return
    getTaskStatus(id)
      .then((r) => {
        setStatusResp(r)
        if (r.status === 'success') {
          loadReport()
        }
      })
      .catch((err) => {
        message.error(errDetail(err, '任务不存在'))
        navigate('/backtests')
      })
  }, [id, loadReport, navigate])

  const running =
    statusResp === null || statusResp.status === 'pending' || statusResp.status === 'running'

  const { progress, message: progressMessage, status: liveStatus } = useTaskProgress(
    running && id ? id : null,
    (finalStatus: TaskStatus) => {
      if (!id) return
      if (finalStatus === 'success') {
        message.success('回测完成')
      }
      getTaskStatus(id)
        .then((r) => {
          setStatusResp(r)
          if (r.status === 'success') {
            loadReport()
          }
        })
        .catch(() => loadReport())
    }
  )

  const displayStatus = liveStatus ?? statusResp?.status ?? 'pending'
  const universe = report?.config?.universe ?? []

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card>
        <Space size="middle" wrap>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/backtests')}>
            返回
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {report?.name ?? '回测任务'}
          </Typography.Title>
          <TaskStatusTag status={displayStatus} />
          <Typography.Text type="secondary">任务ID：{id}</Typography.Text>
        </Space>
        {running && (
          <div style={{ marginTop: 16 }}>
            <Progress
              percent={progress}
              status="active"
              strokeColor={{ from: '#2a6496', to: '#1f4e79' }}
            />
            <Typography.Text type="secondary">
              {progressMessage || '任务执行中，请稍候...'}
            </Typography.Text>
          </div>
        )}
        {(displayStatus === 'failed' || displayStatus === 'cancelled') && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message={displayStatus === 'failed' ? '回测失败' : '任务已取消'}
            description={statusResp?.error || progressMessage || '无详细错误信息'}
          />
        )}
      </Card>

      <Spin spinning={reportLoading}>
        {report ? (
          <Card>
            <Tabs
              destroyInactiveTabPane
              items={[
                {
                  key: 'kline',
                  label: 'K线图',
                  children: (
                    <KLineTab
                      taskId={report.task_id}
                      universe={universe}
                      trades={report.trade_log}
                      reportPeriod={report.config?.period}
                    />
                  )
                },
                {
                  key: 'equity',
                  label: '资金曲线',
                  children: <EquityChart data={report.equity_curve} />
                },
                { key: 'metrics', label: '统计报告', children: <MetricsTab report={report} /> },
                {
                  key: 'trades',
                  label: `交易明细（${report.trade_log?.length ?? 0}）`,
                  children: <TradeLogTab trades={report.trade_log ?? []} />
                },
                {
                  key: 'positions',
                  label: '持仓快照',
                  children: <PositionTab snapshots={report.position_snapshots ?? []} />
                }
              ]}
            />
          </Card>
        ) : (
          !running && (
            <Card>
              <Typography.Text type="secondary">暂无报告数据</Typography.Text>
            </Card>
          )
        )}
      </Spin>
    </Space>
  )
}
