import { Navigate, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { AuthProvider, useAuth } from './context/AuthContext'
import MainLayout from './layouts/MainLayout'
import Login from './pages/Login'
import BacktestList from './pages/BacktestList'
import BacktestResult from './pages/BacktestResult'
import OptimizeList from './pages/OptimizeList'
import OptimizeDetail from './pages/OptimizeDetail'
import AiAnalysis from './pages/AiAnalysis'
import DataManagement from './pages/DataManagement'
import KeyManagement from './pages/KeyManagement'
import UserManagement from './pages/UserManagement'

function RequireAuth({ children }: { children: ReactElement }) {
  const { token } = useAuth()
  if (!token) {
    return <Navigate to="/login" replace />
  }
  return children
}

/** 用户管理页仅 admin 可见（admin 用户名由后端 ADMIN_USERNAME 决定，默认 admin） */
function RequireAdmin({ children }: { children: ReactElement }) {
  const { username } = useAuth()
  if (username !== 'admin') {
    return <Navigate to="/backtests" replace />
  }
  return children
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <MainLayout />
            </RequireAuth>
          }
        >
          <Route index element={<Navigate to="/backtests" replace />} />
          <Route path="backtests" element={<BacktestList />} />
          <Route path="backtests/:id" element={<BacktestResult />} />
          <Route path="optimize" element={<OptimizeList />} />
          <Route path="optimize/:id" element={<OptimizeDetail />} />
          <Route path="ai" element={<AiAnalysis />} />
          <Route path="data" element={<DataManagement />} />
          <Route path="keys" element={<KeyManagement />} />
          <Route
            path="users"
            element={
              <RequireAdmin>
                <UserManagement />
              </RequireAdmin>
            }
          />
        </Route>
        <Route path="*" element={<Navigate to="/backtests" replace />} />
      </Routes>
    </AuthProvider>
  )
}
