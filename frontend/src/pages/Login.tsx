import { useState } from 'react'
import { Button, Card, Form, Input, message } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { errDetail, login as loginApi } from '../api/client'
import { useAuth } from '../context/AuthContext'

export default function Login() {
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()
  const { login } = useAuth()

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      const res = await loginApi(values)
      login(res.token, res.username)
      message.success('登录成功')
      navigate('/backtests')
    } catch (err) {
      message.error(errDetail(err, '登录失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      <div
        style={{
          flex: 1,
          background: 'linear-gradient(135deg, #1f4e79 0%, #2a6496 100%)',
          color: '#fff',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          padding: '0 8%',
          minHeight: '100vh'
        }}
      >
        <h1 style={{ fontSize: 40, color: '#fff', marginBottom: 16 }}>A股量化回测系统</h1>
        <p style={{ fontSize: 16, opacity: 0.85, margin: 0 }}>
          策略回测 · K线复盘 · 参数寻优 · AI 分析 · 数据管理
        </p>
        <p style={{ fontSize: 13, opacity: 0.6, marginTop: 24 }}>
          支持多策略动态参数、精细化交易成本与风控建模、做T与加减仓贡献分解
        </p>
      </div>
      <div
        style={{
          width: 480,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#fff'
        }}
      >
        <Card style={{ width: 360, boxShadow: '0 4px 24px rgba(0,0,0,0.08)' }} title="用户登录">
          <Form layout="vertical" onFinish={onFinish} initialValues={{ username: 'admin' }}>
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input prefix={<UserOutlined />} placeholder="用户名" size="large" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large" loading={loading}>
              登录
            </Button>
          </Form>
        </Card>
      </div>
    </div>
  )
}
