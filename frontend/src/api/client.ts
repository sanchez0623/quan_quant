import axios from 'axios'
import type {
  AiAnalysisItem,
  AiAnalyzeRequest,
  AiProfilesResponse,
  BacktestCreateRequest,
  BacktestListItem,
  BacktestReport,
  DataDemoRequest,
  DataStatus,
  KLineResponse,
  LoginRequest,
  LoginResponse,
  OptimizeCreateRequest,
  OptimizeDetail,
  OptimizeListItem,
  StockItem,
  Strategy,
  TaskCreateResponse,
  TaskStatusResponse
} from './types'

export const TOKEN_KEY = 'quant_token'
export const USERNAME_KEY = 'quant_username'

export const api = axios.create({ baseURL: '/api', timeout: 60000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (axios.isAxiosError(error) && error.response?.status === 401 && !location.pathname.startsWith('/login')) {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USERNAME_KEY)
      location.href = '/login'
    }
    return Promise.reject(error)
  }
)

/** 从 axios 错误中提取后端 detail 字段 */
export function errDetail(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: string } }; message?: string }
  return e?.response?.data?.detail || e?.message || fallback
}

// ---- 认证 ----
export async function login(data: LoginRequest): Promise<LoginResponse> {
  const res = await api.post<LoginResponse>('/auth/login', data)
  return res.data
}

// ---- 策略 ----
export async function getStrategies(): Promise<Strategy[]> {
  const res = await api.get<Strategy[]>('/strategies')
  return res.data
}

// ---- 股票查询 ----
export async function getStocks(keyword: string, limit = 20): Promise<StockItem[]> {
  const res = await api.get<StockItem[]>('/stocks', { params: { keyword, limit } })
  return res.data
}

// ---- 回测任务 ----
export async function createBacktest(data: BacktestCreateRequest): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/backtests', data)
  return res.data
}

export async function getBacktests(): Promise<BacktestListItem[]> {
  const res = await api.get<BacktestListItem[]>('/backtests')
  return res.data
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const res = await api.get<TaskStatusResponse>(`/backtests/${taskId}/status`)
  return res.data
}

export async function getBacktestReport(taskId: string): Promise<BacktestReport> {
  const res = await api.get<BacktestReport>(`/backtests/${taskId}/report`)
  return res.data
}

export async function getKline(taskId: string, code: string): Promise<KLineResponse> {
  const res = await api.get<KLineResponse>(`/backtests/${taskId}/kline`, { params: { code } })
  return res.data
}

// ---- 参数寻优 ----
export async function createOptimize(data: OptimizeCreateRequest): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/optimize', data)
  return res.data
}

export async function getOptimizeList(): Promise<OptimizeListItem[]> {
  const res = await api.get<OptimizeListItem[]>('/optimize')
  return res.data
}

export async function getOptimizeDetail(taskId: string): Promise<OptimizeDetail> {
  const res = await api.get<OptimizeDetail>(`/optimize/${taskId}`)
  return res.data
}

// ---- AI 分析 ----
export async function getAiProfiles(): Promise<AiProfilesResponse> {
  const res = await api.get<AiProfilesResponse>('/ai/profiles')
  return res.data
}

export async function startAiAnalyze(data: AiAnalyzeRequest): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/ai/analyze', data)
  return res.data
}

export async function getAiAnalyses(backtestId: string): Promise<AiAnalysisItem[]> {
  const res = await api.get<AiAnalysisItem[]>('/ai/analyses', { params: { backtest_id: backtestId } })
  return res.data
}

// ---- 数据管理 ----
export async function getDataStatus(): Promise<DataStatus> {
  const res = await api.get<DataStatus>('/data/status')
  return res.data
}

export async function updateData(scope: 'daily' | 'minute5' | 'all'): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/data/update', { scope })
  return res.data
}

export async function createDemoData(data: DataDemoRequest): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/data/demo', data)
  return res.data
}
