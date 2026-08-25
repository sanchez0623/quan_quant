import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
  message
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import {
  createUser,
  deleteUser,
  errDetail,
  getUsers,
  updateUserPassword
} from '../api/client'
import type { UserItem } from '../api/types'
import { USERNAME_KEY } from '../api/client'

/** 用户管理（仅管理员）：创建账号 / 改密 / 删除；回测等功能全体用户共享 */
export default function UserManagement() {
  const [users, setUsers] = useState<UserItem[]>([])
  const [loading, setLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [pwdTarget, setPwdTarget] = useState<UserItem | null>(null)
  const [saving, setSaving] = useState(false)
  const [createForm] = Form.useForm()
  const [pwdForm] = Form.useForm()
  const me = localStorage.getItem(USERNAME_KEY)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setUsers(await getUsers())
    } catch (err) {
      message.error(errDetail(err, '加载用户列表失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const onCreate = async () => {
    const values = await createForm.validateFields()
    setSaving(true)
    try {
      await createUser(values)
      message.success(`用户 ${values.username} 已创建`)
      setCreateOpen(false)
      createForm.resetFields()
      load()
    } catch (err) {
      message.error(errDetail(err, '创建失败'))
    } finally {
      setSaving(false)
    }
  }

  const onPwd = async () => {
    const values = await pwdForm.validateFields()
    if (!pwdTarget) return
    setSaving(true)
    try {
      await updateUserPassword(pwdTarget.username, values.password)
      message.success('密码已重置')
      setPwdTarget(null)
      pwdForm.resetFields()
    } catch (err) {
      message.error(errDetail(err, '重置失败'))
    } finally {
      setSaving(false)
    }
  }

  const onDelete = async (user: UserItem) => {
    try {
      await deleteUser(user.username)
      message.success(`用户 ${user.username} 已删除（其 API Key 一并删除）`)
      load()
    } catch (err) {
      message.error(errDetail(err, '删除失败'))
    }
  }

  const columns: ColumnsType<UserItem> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    {
      title: '用户名',
      dataIndex: 'username',
      width: 180,
      render: (v: string) => (
        <Space>
          <span>{v}</span>
          {v === me && <Tag color="blue">当前登录</Tag>}
          {v === 'admin' && <Tag color="gold">管理员</Tag>}
        </Space>
      )
    },
    { title: '创建时间', dataIndex: 'created_at' },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      render: (_, record) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => setPwdTarget(record)}>
            重置密码
          </Button>
          {record.username !== 'admin' && (
            <Popconfirm
              title={`删除用户 ${record.username}？`}
              description="其私有的 API Key 将一并删除"
              onConfirm={() => onDelete(record)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      )
    }
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title="用户管理"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              创建用户
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
          每位用户登录后管理自己的 LLM API Key（见「Key 管理」页）；回测、寻优、AI
          分析结果、数据等所有功能全体用户共享。
        </Typography.Paragraph>
        <Table<UserItem> rowKey="id" size="small" loading={loading} columns={columns} dataSource={users} pagination={false} />
      </Card>

      <Modal
        title="创建用户"
        open={createOpen}
        onOk={onCreate}
        onCancel={() => setCreateOpen(false)}
        confirmLoading={saving}
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[A-Za-z0-9_]{2,32}$/, message: '2~32位字母/数字/下划线' }
            ]}
          >
            <Input placeholder="例如 friend_a" />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '至少 6 位' }
            ]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`重置密码：${pwdTarget?.username ?? ''}`}
        open={!!pwdTarget}
        onOk={onPwd}
        onCancel={() => setPwdTarget(null)}
        confirmLoading={saving}
        destroyOnClose
      >
        <Form form={pwdForm} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '至少 6 位' }
            ]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
