import { Layout, Dropdown, Avatar, Menu, MenuProps, Space } from 'antd'
import {
  DatabaseOutlined,
  ExperimentOutlined,
  LineChartOutlined,
  LogoutOutlined,
  RobotOutlined,
  UserOutlined
} from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const { Header, Sider, Content } = Layout

const menuItems: MenuProps['items'] = [
  { key: '/backtests', icon: <LineChartOutlined />, label: '回测中心' },
  { key: '/optimize', icon: <ExperimentOutlined />, label: '参数寻优' },
  { key: '/ai', icon: <RobotOutlined />, label: 'AI 分析' },
  { key: '/data', icon: <DatabaseOutlined />, label: '数据管理' }
]

export default function MainLayout() {
  const { username, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const selectedKey = '/' + (location.pathname.split('/')[1] ?? '')

  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key)
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: '#1f4e79',
          paddingInline: 24
        }}
      >
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 600, letterSpacing: 1 }}>
          A股量化回测系统
        </div>
        <Dropdown
          menu={{
            items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
            onClick: ({ key }) => {
              if (key === 'logout') {
                logout()
                navigate('/login')
              }
            }
          }}
        >
          <Space style={{ color: '#fff', cursor: 'pointer' }}>
            <Avatar size="small" icon={<UserOutlined />} style={{ background: '#2a6496' }} />
            <span>{username ?? '用户'}</span>
          </Space>
        </Dropdown>
      </Header>
      <Layout>
        <Sider width={200} theme="light">
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            items={menuItems}
            onClick={handleMenuClick}
            style={{ height: '100%', borderRight: '1px solid #f0f0f0' }}
          />
        </Sider>
        <Content style={{ padding: 16, background: '#f5f6f8' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
