import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import {
  createKey,
  deleteKey,
  errDetail,
  getKeys,
  testKey,
  updateKey
} from '../api/client'
import type { LlmKeyItem, ProviderRegistryEntry } from '../api/types'

interface KeyFormValues {
  provider: string
  api_key?: string
  model?: string | null
  base_url?: string | null
  label?: string
  sort_order?: number
  timeout?: number
  max_tokens?: number
}

/** Key 管理：当前用户私有的 LLM Key 池（增删改、启停、优先级） */
export default function KeyManagement() {
  const [keys, setKeys] = useState<LlmKeyItem[]>([])
  const [registry, setRegistry] = useState<Record<string, ProviderRegistryEntry>>({})
  const [providers, setProviders] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<LlmKeyItem | null>(null)
  const [saving, setSaving] = useState(false)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [form] = Form.useForm<KeyFormValues>()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await getKeys()
      setKeys(res.keys)
      setRegistry(res.registry)
      setProviders(res.providers)
    } catch (err) {
      message.error(errDetail(err, '加载 Key 列表失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const providerLabel = useCallback(
    (p: string) => (p === 'custom' ? '自定义' : (registry[p]?.label ?? p)),
    [registry]
  )

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ provider: 'deepseek', sort_order: keys.length + 1 })
    setModalOpen(true)
  }

  const openEdit = (record: LlmKeyItem) => {
    setEditing(record)
    form.resetFields()
    form.setFieldsValue({
      provider: record.provider,
      model: record.model ?? undefined,
      base_url: record.base_url ?? undefined,
      label: record.label,
      sort_order: record.sort_order,
      timeout: record.timeout ?? undefined,
      max_tokens: record.max_tokens ?? undefined
      // api_key 留空 = 不修改
    })
    setModalOpen(true)
  }

  const onSubmit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (editing) {
        const payload: Record<string, unknown> = {
          provider: values.provider,
          model: values.model ?? null,
          base_url: values.provider === 'custom' ? (values.base_url ?? null) : null,
          label: values.label ?? '',
          sort_order: values.sort_order ?? 0,
          timeout: values.timeout,
          max_tokens: values.max_tokens
        }
        if (values.api_key) payload.api_key = values.api_key // 留空不改 key
        await updateKey(editing.id, payload)
        message.success('Key 已更新')
      } else {
        await createKey({
          provider: values.provider,
          api_key: values.api_key ?? '',
          model: values.provider === 'custom' ? values.model ?? null : values.model ?? null,
          base_url: values.provider === 'custom' ? values.base_url ?? null : null,
          label: values.label ?? '',
          sort_order: values.sort_order ?? 0,
          timeout: values.timeout,
          max_tokens: values.max_tokens
        })
        message.success('Key 已添加')
      }
      setModalOpen(false)
      load()
    } catch (err) {
      message.error(errDetail(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const onToggle = async (record: LlmKeyItem, enabled: boolean) => {
    try {
      await updateKey(record.id, { enabled })
      message.success(enabled ? '已启用' : '已禁用（AI 分析轮换时跳过）')
      load()
    } catch (err) {
      message.error(errDetail(err, '操作失败'))
    }
  }

  const onDelete = async (record: LlmKeyItem) => {
    try {
      await deleteKey(record.id)
      message.success('已删除')
      load()
    } catch (err) {
      message.error(errDetail(err, '删除失败'))
    }
  }

  const onTest = async (record: LlmKeyItem) => {
    setTestingId(record.id)
    try {
      const r = await testKey(record.id)
      message.success(`联通成功 · ${r.model}（${r.elapsed}s）：${r.reply ?? '（无回复内容）'}`)
    } catch (err) {
      message.error(errDetail(err, '测试失败'))
    } finally {
      setTestingId(null)
    }
  }

  const columns: ColumnsType<LlmKeyItem> = useMemo(
    () => [
      {
        title: '优先级',
        dataIndex: 'sort_order',
        width: 80,
        sorter: (a, b) => a.sort_order - b.sort_order,
        defaultSortOrder: 'ascend'
      },
      {
        title: '服务商',
        dataIndex: 'provider',
        width: 130,
        render: (v: string) => (
          <Tag color={v === 'custom' ? 'purple' : 'blue'}>{providerLabel(v)}</Tag>
        )
      },
      {
        title: '模型',
        dataIndex: 'model',
        width: 200,
        render: (v: string | null, record) => v || (registry[record.provider]?.default_model ?? '-')
      },
      { title: '备注', dataIndex: 'label', width: 140, render: (v: string) => v || '-' },
      {
        title: '超时/Tokens',
        key: 'limits',
        width: 120,
        render: (_, record) => (
          <Typography.Text type="secondary">
            {record.timeout ? `${record.timeout}s` : '默认'} /{' '}
            {record.max_tokens ? record.max_tokens : '默认'}
          </Typography.Text>
        )
      },
      {
        title: 'API Key（脱敏）',
        dataIndex: 'api_key',
        width: 160,
        render: (v: string) => <Typography.Text code>{v}</Typography.Text>
      },
      {
        title: '启用',
        dataIndex: 'enabled',
        width: 80,
        render: (v: boolean, record) => (
          <Switch size="small" checked={v} onChange={(checked) => onToggle(record, checked)} />
        )
      },
      {
        title: '操作',
        key: 'actions',
        width: 170,
        render: (_, record) => (
          <Space size="small">
            <Button
              type="link"
              size="small"
              loading={testingId === record.id}
              onClick={() => onTest(record)}
            >
              测试
            </Button>
            <Button type="link" size="small" onClick={() => openEdit(record)}>
              编辑
            </Button>
            <Popconfirm
              title="确定删除该 Key？"
              description="删除后 AI 分析轮换将不再使用它"
              onConfirm={() => onDelete(record)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        )
      }
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [registry, providerLabel, testingId]
  )

  const watchProvider = Form.useWatch('provider', form)

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title="我的 API Key 池"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              添加 Key
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="Key 仅自己可见，用于 AI 分析"
          description="支持 DeepSeek / OpenRouter / 火山方舟 / 智谱 / 硅基流动 / Ollama 及任意 OpenAI 兼容端点（自定义）。按优先级自动轮换：某个 Key 余额不足、失效或限流时无缝切换下一个，跨服务商生效。回测、寻优、数据等其他功能全体用户共享。"
        />
        <Table<LlmKeyItem>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={keys}
          pagination={false}
          locale={{ emptyText: '暂无 Key，点击右上角「添加 Key」配置你的第一个 API Key' }}
        />
      </Card>

      <Modal
        title={editing ? '编辑 Key' : '添加 Key'}
        open={modalOpen}
        onOk={onSubmit}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="provider"
            label="服务商"
            rules={[{ required: true, message: '请选择服务商' }]}
          >
            <Select
              options={providers.map((p) => ({
                value: p,
                label: `${p === 'custom' ? '自定义（任意 OpenAI 兼容端点）' : providerLabel(p)}（${p}）`
              }))}
            />
          </Form.Item>
          {watchProvider === 'custom' && (
            <>
              <Form.Item
                name="base_url"
                label="Base URL"
                rules={[{ required: true, message: '自定义服务商需填写 base_url' }]}
              >
                <Input placeholder="https://api.example.com/v1" />
              </Form.Item>
              <Form.Item
                name="model"
                label="模型名"
                rules={[{ required: true, message: '自定义服务商需填写模型名' }]}
              >
                <Input placeholder="例如 my-model-name" />
              </Form.Item>
            </>
          )}
          {watchProvider !== 'custom' && (
            <Form.Item
              name="model"
              label="模型（留空用默认）"
              extra={
                watchProvider && registry[watchProvider]
                  ? `默认：${registry[watchProvider].default_model}`
                  : undefined
              }
            >
              <Input placeholder={watchProvider ? registry[watchProvider]?.default_model : ''} />
            </Form.Item>
          )}
          <Form.Item
            name="api_key"
            label={editing ? 'API Key（留空表示不修改）' : 'API Key'}
            rules={
              editing
                ? []
                : [{ required: true, message: '请填写 API Key' }, { min: 8, message: 'Key 过短' }]
            }
          >
            <Input.Password placeholder="sk-..." autoComplete="new-password" />
          </Form.Item>
          <Space size="middle" style={{ display: 'flex' }}>
            <Form.Item name="label" label="备注" style={{ flex: 1 }}>
              <Input placeholder="例如：我的主力Key" />
            </Form.Item>
            <Form.Item
              name="sort_order"
              label="优先级"
              tooltip="1=最优先（最先用）。编辑后其他 Key 自动顺延，排序始终为唯一 1..N"
            >
              <InputNumber min={1} style={{ width: 120 }} />
            </Form.Item>
          </Space>
          <Space size="middle" style={{ display: 'flex' }}>
            <Form.Item
              name="timeout"
              label="超时（秒）"
              tooltip="留空用全局默认（300秒）。长输出/大模型建议加大"
              style={{ flex: 1 }}
            >
              <InputNumber min={1} style={{ width: '100%' }} placeholder="默认 300" />
            </Form.Item>
            <Form.Item
              name="max_tokens"
              label="输出 Token 上限"
              tooltip="留空用全局默认（32768）。限制单次回复长度，防止超时"
              style={{ flex: 1 }}
            >
              <InputNumber min={1} step={1024} style={{ width: '100%' }} placeholder="默认 32768" />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </Space>
  )
}
