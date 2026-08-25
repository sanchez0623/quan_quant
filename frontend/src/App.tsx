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

function RequireAuth({ children }: { children: ReactElement }) {
  const { token } = useAuth()
  if (!token) {
    return <Navigate to="/login" replace />
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
        </Route>
        <Route path="*" element={<Navigate to="/backtests" replace />} />
      </Routes>
    </AuthProvider>
  )
}
