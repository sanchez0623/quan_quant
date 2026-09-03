import axios from 'axios'
import type {
  AiAnalysisItem,
  AiAnalyzeRequest,
  AiProfilesResponse,
  BacktestCreateRequest,
  BacktestListItem,
  BacktestReport,
  BacktestTemplateItem,
  BsCheckResult,
  BsMonitor,
  DataDemoRequest,
  DataStatus,
  ExperimentCreateRequest,
  ExperimentDetail,
  LiveConfig,
  LivePremarketResult,
  LivePosition,
  LiveSummary,
  IntradayRunResult,
  IntradayStatus,
  ReadinessResult,
  ShadowStats,
  SlippageResult,
  ExperimentListItem,
  KeyCreateRequest,
  KeyTestResult,
  KeyUpdateRequest,
  KeysResponse,
  KLineResponse,
  LoginRequest,
  LoginResponse,
  OptimizeCreateRequest,
  OptimizeDetail,
  OptimizeListItem,
  PickOptions,
  PickRequest,
  PickResponse,
  StockItem,
  Strategy,
  TaskCreateResponse,
  TaskStatusResponse,
  TemplateCreateRequest,
  UserCreateRequest,
  UserItem
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

/** 按代码批量查询股票（支持逗号/空格/换行分隔，兼容 sh./sz. 前缀） */
export async function getStocksByCodes(codes: string[]): Promise<StockItem[]> {
  const res = await api.get<StockItem[]>('/stocks/by-codes', { params: { codes: codes.join(',') } })
  return res.data
}

// ---- 条件选股（UNIVERSE_PICKER）----
/** 获取条件选股筛选维度选项（指数/行业树/板块 + 快照日期） */
export async function getPickOptions(): Promise<PickOptions> {
  const res = await api.get<PickOptions>('/stocks/pick-options')
  return res.data
}

/** 条件选股：过滤 + 可复现随机抽样（同 seed 同池子） */
export async function pickStocks(data: PickRequest): Promise<PickResponse> {
  const res = await api.post<PickResponse>('/stocks/pick', data)
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

/** 删除回测任务（运行中的任务会被后端拒绝） */
export async function deleteBacktest(taskId: string): Promise<{ status: string }> {
  const res = await api.delete<{ status: string }>(`/backtests/${taskId}`)
  return res.data
}

// ---- 回测配置模板（每用户私有） ----
export async function getTemplates(): Promise<BacktestTemplateItem[]> {
  const res = await api.get<BacktestTemplateItem[]>('/backtests/templates')
  return res.data
}

export async function createTemplate(
  data: TemplateCreateRequest
): Promise<{ id: number; status: string }> {
  const res = await api.post<{ id: number; status: string }>('/backtests/templates', data)
  return res.data
}

export async function deleteTemplate(templateId: number): Promise<{ status: string }> {
  const res = await api.delete<{ status: string }>(`/backtests/templates/${templateId}`)
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

export async function getKline(taskId: string, code: string, period?: string): Promise<KLineResponse> {
  const res = await api.get<KLineResponse>(`/backtests/${taskId}/kline`, {
    params: { code, ...(period ? { period } : {}) }
  })
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

/** 断点续传：同一 task_id 重提，Optuna 载入既有 trial 续跑 */
export async function resumeOptimize(taskId: string): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>(`/optimize/${taskId}/resume`)
  return res.data
}

// ---- 对比实验 ----
export async function createExperiment(
  data: ExperimentCreateRequest
): Promise<{ experiment_id: string; sub_task_ids: string[]; status: string }> {
  const res = await api.post('/experiments', data)
  return res.data
}

export async function getExperimentList(): Promise<ExperimentListItem[]> {
  const res = await api.get<ExperimentListItem[]>('/experiments')
  return res.data
}

export async function getExperimentDetail(expId: string): Promise<ExperimentDetail> {
  const res = await api.get<ExperimentDetail>(`/experiments/${expId}`)
  return res.data
}

export async function deleteExperiment(expId: string): Promise<{ ok: boolean }> {
  const res = await api.delete(`/experiments/${expId}`)
  return res.data
}

// ---- AI 分析 ----
export async function getAiProfiles(): Promise<AiProfilesResponse> {
  const res = await api.get<AiProfilesResponse>('/ai/profiles')
  return res.data
}

export async function clearAiUsage(): Promise<{ status: string }> {
  const res = await api.delete<{ status: string }>('/ai/usage')
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

// ---- Key 管理（每用户私有 Key 池） ----
export async function getKeys(): Promise<KeysResponse> {
  const res = await api.get<KeysResponse>('/keys')
  return res.data
}

export async function createKey(data: KeyCreateRequest): Promise<{ id: number; status: string }> {
  const res = await api.post<{ id: number; status: string }>('/keys', data)
  return res.data
}

export async function updateKey(keyId: number, data: KeyUpdateRequest): Promise<{ status: string }> {
  const res = await api.put<{ status: string }>(`/keys/${keyId}`, data)
  return res.data
}

export async function deleteKey(keyId: number): Promise<{ status: string }> {
  const res = await api.delete<{ status: string }>(`/keys/${keyId}`)
  return res.data
}

export async function testKey(keyId: number): Promise<KeyTestResult> {
  const res = await api.post<KeyTestResult>(`/keys/${keyId}/test`)
  return res.data
}

// ---- 用户管理（仅 admin） ----
export async function getUsers(): Promise<UserItem[]> {
  const res = await api.get<UserItem[]>('/users')
  return res.data
}

export async function createUser(data: UserCreateRequest): Promise<{ status: string }> {
  const res = await api.post<{ status: string }>('/users', data)
  return res.data
}

export async function updateUserPassword(username: string, password: string): Promise<{ status: string }> {
  const res = await api.put<{ status: string }>(`/users/${username}/password`, { password })
  return res.data
}

export async function deleteUser(username: string): Promise<{ status: string }> {
  const res = await api.delete<{ status: string }>(`/users/${username}`)
  return res.data
}

// ---- 数据管理 ----
export async function getDataStatus(): Promise<DataStatus> {
  const res = await api.get<DataStatus>('/data/status')
  return res.data
}

export async function getBsMonitor(): Promise<BsMonitor> {
  const res = await api.get<BsMonitor>('/data/bs_monitor')
  return res.data
}

export async function checkBs(): Promise<BsCheckResult> {
  const res = await api.post<BsCheckResult>('/data/bs_check')
  return res.data
}

// ---- 实盘信号机（LIVE_SIGNAL_SYSTEM）----
export async function getLiveSummary(): Promise<LiveSummary> {
  const res = await api.get<LiveSummary>('/live/summary')
  return res.data
}

export async function runPremarket(): Promise<LivePremarketResult> {
  const res = await api.post<LivePremarketResult>('/live/premarket')
  return res.data
}

export async function setLiveSignalStatus(id: number, status: string): Promise<void> {
  await api.post(`/live/signals/${id}/status`, { status })
}

export async function addLiveFill(body: {
  signal_id?: number | null
  code: string
  side: 'buy' | 'sell'
  fill_price: number
  fill_volume: number
  fee?: number
  note?: string
}): Promise<{ fill_id: number; positions: LivePosition[] }> {
  const res = await api.post('/live/fills', body)
  return res.data
}

export async function syncLivePositions(
  positions: Array<{ code: string; name?: string; volume: number; cost_price: number }>
): Promise<{ positions: LivePosition[] }> {
  const res = await api.post('/live/positions/sync', { positions })
  return res.data
}

export async function getLiveConfig(): Promise<LiveConfig> {
  const res = await api.get<LiveConfig>('/live/config')
  return res.data
}

export async function saveLiveConfig(cfg: LiveConfig): Promise<LiveConfig> {
  const res = await api.post<LiveConfig>('/live/config', cfg)
  return res.data
}

export async function resetLiveData(keepConfig = true): Promise<void> {
  await api.post('/live/reset', { keep_config: keepConfig })
}

// ---- 盘中信号机（M2）----
export async function runMorning(updateData = true): Promise<{ task_id: string }> {
  const res = await api.post('/live/morning', { update_data: updateData })
  return res.data
}

export async function runIntraday(): Promise<IntradayRunResult> {
  const res = await api.post<IntradayRunResult>('/live/intraday')
  return res.data
}

export async function getIntradayStatus(): Promise<IntradayStatus> {
  const res = await api.get<IntradayStatus>('/live/intraday/status')
  return res.data
}

export async function runPostclose(): Promise<{ task_id: string }> {
  const res = await api.post('/live/postclose')
  return res.data
}

// ---- 滑点 / 影子（M3）/ 就绪（M4）----
export async function getSlippage(): Promise<SlippageResult> {
  const res = await api.get<SlippageResult>('/live/slippage')
  return res.data
}

export async function getShadowStats(): Promise<ShadowStats> {
  const res = await api.get<ShadowStats>('/live/shadow')
  return res.data
}

export async function getReadiness(): Promise<ReadinessResult> {
  const res = await api.get<ReadinessResult>('/live/readiness')
  return res.data
}

export async function updateData(
  scope: 'daily' | 'minute5' | 'all' | 'industry' | 'stock_basic' | 'calendar' | 'index_daily',
  stocks?: string[],
  dateRange?: { startDate?: string; endDate?: string }
): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/data/update', {
    scope,
    stocks,
    start_date: dateRange?.startDate ?? null,
    end_date: dateRange?.endDate ?? null
  })
  return res.data
}

export async function createDemoData(data: DataDemoRequest): Promise<TaskCreateResponse> {
  const res = await api.post<TaskCreateResponse>('/data/demo', data)
  return res.data
}
