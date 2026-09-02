import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Input,
  InputNumber,
  message,
  Modal,
  Progress,
  Radio,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography
} from 'antd'
import type { Dayjs } from 'dayjs'
import { DatabaseOutlined, SyncOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { checkBs, createDemoData, errDetail, getBsMonitor, getDataStatus, updateData } from '../api/client'
import type { BsMonitor, DataSourceHealth } from '../api/types'
import { useTaskProgress } from '../hooks/useTaskProgress'
import { fmtInt } from '../utils/format'

function HealthyTag({ healthy }: { healthy: boolean | null }) {
  if (healthy === true) return <Tag color="success">正常</Tag>
  if (healthy === false) return <Tag color="error">异常</Tag>
  return <Tag color="default">未检测</Tag>
}

export default function DataManagement() {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof getDataStatus>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [scope, setScope] = useState<'daily' | 'minute5' | 'all'>('daily')
  const [task, setTask] = useState<{ id: string; label: string } | null>(null)
  const [demoDays, setDemoDays] = useState<number>(500)
  const [stocksInput, setStocksInput] = useState('')
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null)
  const [bs, setBs] = useState<BsMonitor | null>(null)
  const [bsLoading, setBsLoading] = useState(false)

  const refreshBs = useCallback(async () => {
    try {
      setBs(await getBsMonitor())
    } catch (err) {
      message.error(errDetail(err, '加载 baostock 监控失败'))
    }
  }, [])

  useEffect(() => {
    refreshBs()
  }, [refreshBs])

  const onBsCheck = async () => {
    setBsLoading(true)
    try {
      const res = await checkBs()
      setBs(res.monitor)
      if (res.ok) {
        message.success('baostock 连接正常')
      } else if (res.monitor.blacklisted) {
        message.warning(`baostock 已被黑名单限制，预计 ${res.monitor.release_at ?? '未知时间'} 解除`)
      } else {
        message.warning('baostock 健康检查异常（可能未安装或网络问题）')
      }
    } catch (err) {
      message.error(errDetail(err, '检查失败'))
    } finally {
      setBsLoading(false)
    }
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await getDataStatus())
    } catch (err) {
      message.error(errDetail(err, '加载数据状态失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const { progress, message: taskMessage, status: taskStatus } = useTaskProgress(task?.id ?? null, (s, fullState) => {
    const label = task?.label ?? '任务'
    if (s === 'success') {
      message.success(`${label}完成`)
    } else {
      // 显示具体错误信息
      const errMsg = fullState?.message || `${label}失败`
      message.error(errMsg)
    }
    setTask(null)
    refresh()
  })

  const [elapsedSec, setElapsedSec] = useState(0)
  useEffect(() => {
    if (!task) return
    setElapsedSec(0)
    const startedAt = Date.now()
    const timer = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => clearInterval(timer)
  }, [task])

  const onUpdate = async () => {
    const stocks = stocksInput
      .split(/[,，\s;；]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    try {
      const res = await updateData(
        scope,
        stocks.length > 0 ? stocks : undefined,
        dateRange
          ? {
              startDate: dateRange[0]?.format('YYYY-MM-DD') || undefined,
              endDate: dateRange[1]?.format('YYYY-MM-DD') || undefined
            }
          : undefined
      )
      setTask({ id: res.task_id, label: '数据更新' })
      message.info(
        stocks.length > 0 ? `更新任务已提交（指定 ${stocks.length} 只）` : '更新任务已提交（全量）'
      )
    } catch (err) {
      message.error(errDetail(err, '提交更新失败'))
    }
  }

  /** 更新行业与成分（申万三级 + 指数成分），无需指定股票/日期 */
  const onUpdateIndustry = async () => {
    try {
      const res = await updateData('industry')
      setTask({ id: res.task_id, label: '行业与成分更新' })
      message.info('行业与成分更新任务已提交（申万三级约需 3~5 分钟）')
    } catch (err) {
      message.error(errDetail(err, '提交更新失败'))
    }
  }

  /** 更新股票列表（ST/退市标记）：baostock query_all_stock 拉当前在市集合 */
  const onUpdateStockBasic = async () => {
    try {
      const res = await updateData('stock_basic')
      setTask({ id: res.task_id, label: '股票列表更新' })
      message.info('股票列表更新任务已提交（拉取全市场在市证券，秒级）')
    } catch (err) {
      message.error(errDetail(err, '提交更新失败'))
    }
  }

  const onDemo = () => {
    Modal.confirm({
      title: '确认生成演示数据？',
      content: (
        <div>
          <p>将生成合成数据（随机游走+趋势）供无真实数据源环境的联调与演示。</p>
          <p>幂等：重复调用会覆盖生成。</p>
        </div>
      ),
      okText: '生成',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await createDemoData({ days: demoDays })
          setTask({ id: res.task_id, label: '演示数据生成' })
          message.info('生成任务已提交')
        } catch (err) {
          message.error(errDetail(err, '提交生成失败'))
        }
      }
    })
  }

  const sourceColumns: ColumnsType<DataSourceHealth> = [
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '角色', dataIndex: 'role' },
    {
      title: '健康状态',
      dataIndex: 'healthy',
      width: 110,
      render: (v: boolean | null) => <HealthyTag healthy={v} />
    },
    {
      title: '最后检查时间',
      dataIndex: 'last_check',
      width: 180,
      render: (v: string | null) => v ?? '-'
    },
    { title: '备注', dataIndex: 'note', render: (v: string) => v || '-' }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={6}>
          <Card size="small" title="日线（daily）" loading={loading}>
            <Statistic title="股票数" value={fmtInt(status?.daily.stocks)} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="数据行数"
              value={fmtInt(status?.daily.rows)}
              valueStyle={{ fontSize: 20 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              起止：{status?.daily.start ?? '-'} ~ {status?.daily.end ?? '-'}
              <br />
              更新：{status?.daily.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="5分钟线（minute5）" loading={loading}>
            <Statistic title="股票数" value={fmtInt(status?.minute5.stocks)} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="数据行数"
              value={fmtInt(status?.minute5.rows)}
              valueStyle={{ fontSize: 20 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              起止：{status?.minute5.start ?? '-'} ~ {status?.minute5.end ?? '-'}
              <br />
              更新：{status?.minute5.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="复权因子（adj_factor）" loading={loading}>
            <Statistic title="数据行数" value={fmtInt(status?.adj_factor.rows)} valueStyle={{ fontSize: 20 }} />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12, marginTop: 8 }}>
              更新：{status?.adj_factor.updated_at ?? '-'}
            </Typography.Paragraph>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" title="交易日历（calendar）" loading={loading}>
            <Statistic
              title="起止日期"
              value={`${status?.calendar.start ?? '-'} ~ ${status?.calendar.end ?? '-'}`}
              valueStyle={{ fontSize: 16 }}
            />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="数据源健康状态">
        <Table<DataSourceHealth>
          rowKey="name"
          size="small"
          pagination={false}
          loading={loading}
          dataSource={status?.sources ?? []}
          columns={sourceColumns}
        />
      </Card>

      {/* baostock API 调用监控：今日用量 vs 上限 / 并发连接 / 黑名单状态 */}
      <Card
        size="small"
        title="baostock API 调用监控"
        extra={
          <Button size="small" icon={<SyncOutlined />} loading={bsLoading} onClick={onBsCheck}>
            立即检查
          </Button>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Space size="large" wrap>
            <Statistic
              title="今日调用 / 上限"
              value={bs ? `${fmtInt(bs.today_count)} / ${fmtInt(bs.cap)}` : '-'}
              valueStyle={{
                fontSize: 18,
                color: bs && bs.today_count / bs.cap > 0.8 ? '#fa8c16' : undefined
              }}
            />
            <Statistic title="今年被限制次数" value={bs?.freeze_count ?? '-'} valueStyle={{ fontSize: 18 }} />
            <Statistic title="当前IP" value={bs?.ip || '-'} valueStyle={{ fontSize: 16 }} />
            <Statistic
              title="并发连接"
              value={bs ? (bs.concurrency > 0 ? 1 : 0) : '-'}
              valueStyle={{ fontSize: 18 }}
            />
          </Space>
          <Progress
            percent={bs ? Math.min(100, Math.round((bs.today_count / bs.cap) * 100)) : 0}
            status={bs && bs.today_count / bs.cap > 0.8 ? 'exception' : 'normal'}
            format={() =>
              bs ? `${bs.today_count.toLocaleString('zh-CN')} / ${bs.cap.toLocaleString('zh-CN')}` : '-'
            }
          />
          {bs?.blacklisted ? (
            <Alert
              type="error"
              showIcon
              message={`IP 已被 baostock 黑名单限制（今年第 ${bs.freeze_count} 次）`}
              description={
                bs.release_at
                  ? `预计 ${bs.release_at} 自动解除；限制时长 = 今年累计次数 × 6 小时`
                  : '待释放时间为空，请等待 5 分钟后刷新页面'
              }
            />
          ) : (
            <Alert
              type="success"
              showIcon
              message="baostock 访问正常（串行锁生效：同一时刻仅 1 个连接）"
            />
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            规则：每日请求 ≤ {bs?.cap ?? 50000} 次，超限拒绝新请求；首次被限制冻结 6 小时自动解除，多次限制时长 = 累计次数 × 6 小时。
            {bs?.hint ? ` ${bs.hint}` : ''}
          </Typography.Text>
        </Space>
      </Card>

      <Card size="small" title="数据操作">
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div>
            <Typography.Text strong>增量更新</Typography.Text>
            <div style={{ marginTop: 8 }}>
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <Space>
                  <Radio.Group
                    value={scope}
                    onChange={(e) => setScope(e.target.value)}
                    optionType="button"
                    options={[
                      { value: 'daily', label: '日线' },
                      { value: 'minute5', label: '5分钟线' },
                      { value: 'all', label: '全部' }
                    ]}
                  />
                  <DatePicker.RangePicker
                    value={dateRange}
                    onChange={(dates) => setDateRange(dates)}
                    allowClear
                    style={{ width: 260 }}
                    placeholder={['开始日期', '结束日期']}
                  />
                  <Button
                    type="primary"
                    icon={<SyncOutlined />}
                    disabled={!!task}
                    loading={!!task}
                    onClick={onUpdate}
                  >
                    开始更新
                  </Button>
                </Space>
                <Space.Compact style={{ width: 480 }}>
                  <Input
                    placeholder="指定股票代码（可选，逗号/空格分隔），如：600021, 600000；留空=更新全部股票"
                    value={stocksInput}
                    onChange={(e) => setStocksInput(e.target.value)}
                    allowClear
                  />
                </Space.Compact>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  日期留空=拉取全历史；指定日期则只拉该区间（5分钟线受数据源约 2 年深度限制）。
                  只回测少数股票时建议指定代码按需拉取（如 600021），全量更新（约 5500 只）耗时较长。
                </Typography.Text>
              </Space>
            </div>
          </div>
          <div>
            <Typography.Text strong>生成演示数据</Typography.Text>
            <div style={{ marginTop: 8 }}>
              <Space>
                <Typography.Text type="secondary">天数：</Typography.Text>
                <InputNumber min={30} max={5000} value={demoDays} onChange={(v) => setDemoDays(v ?? 500)} />
                <Button danger icon={<ThunderboltOutlined />} disabled={!!task} onClick={onDemo}>
                  生成演示数据
                </Button>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  生成合成数据（随机游走+趋势），供无真实数据源环境联调演示，重复调用覆盖生成。
                </Typography.Text>
              </Space>
            </div>
          </div>
          {task && (
            <div>
              <Space size="small">
                <Typography.Text strong>{task.label}进行中</Typography.Text>
                <Typography.Text type="secondary">
                  已耗时 {Math.floor(elapsedSec / 60)}:{String(elapsedSec % 60).padStart(2, '0')}
                </Typography.Text>
              </Space>
              <Progress percent={progress} status="active" style={{ maxWidth: 480 }} />
              <div>
                <Typography.Text type="secondary">{taskMessage || '执行中...'}</Typography.Text>
              </div>
            </div>
          )}
        </Space>
      </Card>

      <Card size="small" title="股票列表（ST / 退市标记）">
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          <Space size="large" wrap>
            <Statistic
              title="股票总数"
              value={fmtInt(status?.stock_basic?.total)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="ST 股"
              value={fmtInt(status?.stock_basic?.st_count)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="退市股"
              value={fmtInt(status?.stock_basic?.delisted_count)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="更新于"
              value={status?.stock_basic?.updated_at ?? '未更新'}
              valueStyle={{ fontSize: 16 }}
            />
          </Space>
          <Space>
            <Button
              type="primary"
              icon={<SyncOutlined />}
              disabled={!!task}
              loading={!!task}
              onClick={onUpdateStockBasic}
            >
              更新股票列表
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              拉取 baostock 全市场在市证券（秒级），标记 ST 与退市股。搜索/选股时自动剔除。
            </Typography.Text>
          </Space>
        </Space>
      </Card>

      <Card size="small" title="行业与成分（申万三级 + 指数成分）">
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          <Space size="large" wrap>
            <Statistic
              title="指数成分快照"
              value={status?.index?.snapshot_date ?? '未更新'}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="申万行业快照"
              value={status?.industry?.snapshot_date ?? '未更新'}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="指数成分覆盖"
              value={fmtInt(status?.index?.stocks)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title="申万三级行业数"
              value={fmtInt(status?.industry?.l3_count)}
              valueStyle={{ fontSize: 16 }}
            />
          </Space>
          <Space>
            <Button
              type="primary"
              icon={<DatabaseOutlined />}
              disabled={!!task}
              loading={!!task}
              onClick={onUpdateIndustry}
            >
              更新行业与成分
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              拉取 baostock 上证50/沪深300/中证500 成分（+中证800派生）与乐咕申万 2021 三级行业，
              约 3~5 分钟。供「新建回测 → 条件选股」按行业/指数筛选。月度级变动，手动触发即可。
            </Typography.Text>
          </Space>
        </Space>
      </Card>
    </Space>
  )
}
