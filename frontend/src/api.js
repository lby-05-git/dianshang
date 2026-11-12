// 后端 API 封装（docs/06 ）：JWT 注入、401 登出、SSE 订阅（带鉴权头）
import axios from 'axios'

const configuredApiBase = import.meta.env.VITE_API_BASE_URL
const developmentApiBase = configuredApiBase && /^https?:\/\//.test(configuredApiBase)
  ? configuredApiBase
  : 'http://127.0.0.1:8001/api/v1'

export const API_BASE_URL = (
  import.meta.env.DEV ? developmentApiBase : (configuredApiBase || '/api/v1')
).replace(/\/$/, '')

const api = axios.create({ baseURL: API_BASE_URL, timeout: 20000 })

api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem('token')
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401 && location.hash !== '#/login') {
      localStorage.removeItem('token')
      location.hash = '#/login'
    }
    return Promise.reject(err)
  }
)

export const login = (username, password) =>
  api.post('/auth/login', { username, password })

export const createRefund = (formData, idemKey) =>
  api.post('/refunds', formData, { headers: { 'X-Idempotency-Key': idemKey } })

export const listCases = (params) => api.get('/refunds', { params })

export const getCase = (id) => api.get(`/refunds/${id}`)

// 订单凭证包含敏感信息，必须通过携带 JWT 的请求读取，不能直接公开 uploads 目录。
export const getCaseEvidence = (id) =>
  api.get(`/refunds/${id}/evidence`, { responseType: 'blob' })

export const getCaseEvidences = (id) => api.get(`/refunds/${id}/evidences`)

export const getCaseEvidenceById = (caseId, evidenceId) =>
  api.get(`/refunds/${caseId}/evidences/${evidenceId}`, { responseType: 'blob' })

export const decide = (id, action, opinion, idemKey) =>
  api.post(`/refunds/${id}/${action}`, new URLSearchParams({ opinion }), {
    headers: { 'X-Idempotency-Key': idemKey },
  })

export const getStats = () => api.get('/dashboard/stats')

// ===== 策略中心（3.3.4）=====
export const getPolicyProfiles = () => api.get('/policy/profiles')
export const getPolicyBindings = () => api.get('/policy/bindings')
export const getEffectivePolicy = (channel) => api.get(`/policy/effective/${channel}`)
export const updatePolicyProfile = (profileId, body) => api.put(`/policy/profiles/${profileId}`, body)
export const bindPolicyChannel = (body) => api.post('/policy/bindings', body)

// ===== 班组协同台（3.3.1 /workspace）=====
export const getWorkspaceQueue = () => api.get('/workspace/queue')
export const claimCase = (caseId) => api.post(`/workspace/${caseId}/claim`)
export const transferCase = (caseId, targetUserId) =>
  api.post(`/workspace/${caseId}/transfer`, { target_user_id: targetUserId })
export const getCaseNotes = (caseId) => api.get(`/workspace/${caseId}/notes`)
export const postCaseNote = (caseId, content) =>
  api.post(`/workspace/${caseId}/notes`, { content })

// ===== 高级检索（3.3.2 /search）=====
export const searchCases = (params) => api.get('/search/cases', { params })
export const listSavedSearches = () => api.get('/search/saved')
export const saveSearch = (body) => api.post('/search/saved', body)
export const deleteSavedSearch = (filterId) => api.delete(`/search/saved/${filterId}`)
export const exportCases = (params) =>
  api.get('/search/export', { params, responseType: 'blob' })

// ===== 评测中心（Evaluation Suite）=====
// 数据集
export const evalListSuites = () => api.get('/evaluation-center/datasets')
export const evalCreateSuite = (name, changelog) =>
  api.post('/evaluation-center/datasets', { name, changelog })
export const evalGetSuite = (suiteId) => api.get(`/evaluation-center/datasets/${suiteId}`)
export const evalGetSuiteCases = (suiteId) => api.get(`/evaluation-center/datasets/${suiteId}/cases`)
export const evalCopySuite = (suiteId, newName, changelog, edits = []) =>
  api.post(`/evaluation-center/datasets/${suiteId}/copy`, { new_name: newName, changelog, edits })
export const evalDeleteSuite = (suiteId) => api.delete(`/evaluation-center/datasets/${suiteId}`)

// Runs（评估任务）
export const evalCreateRun = (body) => api.post('/evaluation-center/runs', body)
export const evalCreateOneClickRun = () => api.post('/evaluation-center/runs/one-click')
export const evalListRuns = () => api.get('/evaluation-center/runs')
export const evalGetRun = (runId) => api.get(`/evaluation-center/runs/${runId}`)
export const evalGetRunResults = (runId) => api.get(`/evaluation-center/runs/${runId}/results`)
export const evalCancelRun = (runId) => api.post(`/evaluation-center/runs/${runId}/cancel`)
export const evalGetLatestScorecard = () => api.get('/evaluation-center/scorecard/latest')

// 意图周期对照评测（纯 LLM vs 双层路由）
export const evalGetLatestIntentEvaluation = () =>
  api.get('/evaluation-center/intent-evaluation/latest')
export const evalListIntentEvaluationRuns = (limit = 20) =>
  api.get('/evaluation-center/intent-evaluation/runs', { params: { limit } })
export const evalCreateIntentEvaluation = () =>
  api.post('/evaluation-center/intent-evaluation/runs')

// 报告
export const evalListReports = () => api.get('/evaluation-center/reports')
export const evalGetReport = (runId) => api.get(`/evaluation-center/reports/${runId}`)
export const evalCompareReports = (runA, runB) =>
  api.get('/evaluation-center/reports/compare', { params: { run_a: runA, run_b: runB } })
export const evalExportReport = (runId, fmt = 'xlsx') =>
  api.get(`/evaluation-center/reports/${runId}/export`, { params: { fmt }, responseType: 'blob' })

// Prompt 版本（人工批准门）
export const evalListPrompts = () => api.get('/evaluation-center/prompts')
export const evalCreatePrompt = (body) => api.post('/evaluation-center/prompts', body)
export const evalTransitionPrompt = (promptId, toStatus) =>
  api.post(`/evaluation-center/prompts/${promptId}/transition`, { to_status: toStatus })

// A-B 实验
export const evalCreateAB = (body) => api.post('/evaluation-center/runs/ab', body)
export const evalListAB = () => api.get('/evaluation-center/runs/ab')
export const evalGetABReport = (bundleId) => api.get(`/evaluation-center/reports/ab/${bundleId}`)

// Office 沙箱导入
export const evalImportPreview = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return api.post('/evaluation-center/office/import-preview', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
export const evalImportConfirm = (body) => api.post('/evaluation-center/office/import-confirm', body)

// M18 分级调度（smoke/core/full × manual/daily/weekly/change）
export const evalListSchedules = (enabledOnly = false) =>
  api.get('/evaluation-center/schedules', { params: { enabled_only: enabledOnly } })
export const evalCreateSchedule = (body) => api.post('/evaluation-center/schedules', body)
export const evalGetSchedule = (scheduleId) => api.get(`/evaluation-center/schedules/${scheduleId}`)
export const evalUpdateSchedule = (scheduleId, enabled) =>
  api.patch(`/evaluation-center/schedules/${scheduleId}`, { enabled })
export const evalFireSchedule = (scheduleId) =>
  api.post(`/evaluation-center/schedules/${scheduleId}/fire`)

// M19 发布门禁 + 候选回放
export const evalGetBaseline = (suiteId) => api.get(`/evaluation-center/gate/baseline/${suiteId}`)
export const evalApproveBaseline = (suiteId, runId, note = '') =>
  api.post('/evaluation-center/gate/baseline', { suite_id: suiteId, run_id: runId, note })
export const evalEvaluateGate = (runId, suiteId, tier = 'full', baselineRunId = undefined) =>
  api.post('/evaluation-center/gate/evaluate', {
    run_id: runId, suite_id: suiteId, tier,
    ...(baselineRunId ? { baseline_run_id: baselineRunId } : {}),
  })
export const evalListGateResults = () => api.get('/evaluation-center/gate/results')
export const evalBuildReplaySet = (suiteId, runId, tier) =>
  api.post('/evaluation-center/gate/replay-set', { suite_id: suiteId, run_id: runId, tier })
export const evalGetReplayCases = (suiteId, tier) =>
  api.get(`/evaluation-center/gate/replay-set/${suiteId}/${tier}`)

// M20 漂移监测 + 成本预算告警
export const evalListBudgets = () => api.get('/evaluation-center/monitor/budgets')
export const evalUpsertBudget = (body) => api.post('/evaluation-center/monitor/budgets', body)
export const evalListDrift = () => api.get('/evaluation-center/monitor/drift')
export const evalDriftSummary = (suiteId) =>
  api.get(`/evaluation-center/monitor/drift/summary/${suiteId}`)
export const evalListAlerts = (unacked = false) =>
  api.get('/evaluation-center/monitor/alerts', { params: { unacked } })
export const evalCheckBudget = (body) => api.post('/evaluation-center/monitor/alerts/check', body)
export const evalAckAlert = (alertId) =>
  api.post(`/evaluation-center/monitor/alerts/${alertId}/ack`)

// M21 插件契约：能力注册表（治理页能力驱动导航）
export const evalGetCapabilities = () => api.get('/evaluation-center/capabilities')

// ===== 零信任安全运营=====
export const securityListEvents = (params = {}) => api.get('/security/events', { params })
export const securityGetEventFlow = () => api.get('/security/events/flow')
export const securityDisposeEvent = (eventId, action, note = '') =>
  api.put('/security/events/' + eventId + '/dispose', { action, note })
export const securityCritic = (body) => api.post('/security/llm-critic', body)
export const securityMask = (body) => api.post('/security/ner-mask', body)
export const securityToolGate = (caseId) => api.post('/security/tool-gate', { case_id: caseId })

// ===== 意图识别与可靠性=====
export const intentRoute = (text, caseId = undefined) =>
  api.post('/intent/route', null, { params: { text, ...(caseId ? { case_id: caseId } : {}) } })
export const intentAnalyzeMulti = (text) => api.post('/intent/multi', { text })
export const intentGetMetrics = (days = 7) => api.get('/intent/metrics', { params: { days } })
export const intentListFeedback = (status = undefined) =>
  api.get('/intent/feedback', { params: status ? { status } : {} })
export const intentReviewFeedback = (id, decision, reason = '') =>
  api.put('/intent/feedback/' + id + '/review', { decision, reason })
export const intentListDeadLetters = (params = {}) => api.get('/dead-letter', { params })
export const intentReplayDeadLetter = (id) => api.post('/dead-letter/' + id + '/replay')

// ===== 实时语音与客服陪练=====
export const voiceGetMetrics = () => api.get('/voice/metrics')
export const voiceListScenarios = () => api.get('/voice/coaching/scenarios')
export const voiceGetReplay = (sessionId) => api.get('/voice/coaching/replay/' + sessionId)
export const voiceRecoverSession = (sessionId) => {
  const fd = new FormData()
  fd.append('session_id', sessionId)
  return api.post('/voice/ws/recovery', fd)
}
export const voiceStartCoaching = (sceneId, difficulty, emotion) => {
  const fd = new FormData()
  fd.append('scene_id', sceneId)
  if (difficulty) fd.append('difficulty', difficulty)
  if (emotion) fd.append('emotion', emotion)
  return api.post('/voice/coaching/start', fd)
}
export const voiceScoreCoaching = (sessionId, turns) => {
  const fd = new FormData()
  fd.append('coaching_session_id', sessionId)
  fd.append('turns', JSON.stringify(turns))
  return api.post('/voice/coaching/score', fd)
}

export function apiWebSocketUrl(path) {
  const target = new URL(API_BASE_URL + path, window.location.origin)
  target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:'
  return target.toString()
}

// ===== 订单退款对账与统一版本治理（V3.0）=====
export const reconGetOverview = () => api.get('/reconciliation/overview')
export const reconGetMetrics = () => api.get('/reconciliation/metrics')
export const reconRun = (windowMinutes = 60) =>
  api.post('/reconciliation/run', { window_minutes: windowMinutes })
export const reconListDiffs = (params = {}) => api.get('/reconciliation/diffs', { params })
export const reconListAlerts = () => api.get('/reconciliation/alerts')
export const reconResolveAlert = (eventId) => api.post('/reconciliation/alerts/' + eventId + '/resolve')
export const versionListDomains = () => api.get('/governance/versions/domains')
export const versionList = (params = {}) => api.get('/governance/versions', { params })
export const versionApprove = (id) => api.post('/governance/versions/' + id + '/approve')
export const versionReject = (id) => api.post('/governance/versions/' + id + '/reject')

// SSE 订阅：EventSource 无法携带 Authorization 头 → token 走 ?token= 查询参数
// （后端双通道鉴权，MVP 取舍见 routes_dashboard.py 注释）。
// EventSource 自带断线自动重连（无需手写）；事件合并：500ms 批量只回调一次（防刷新风暴）。
// 不用 fetch 流式读取的原因：fetch 流会让浏览器标签页一直处于"加载中"状态（刷新图标转不停）。
export function subscribeCaseEvents(onEvent) {
  let cancelled = false
  let batchTimer = null

  const emit = () => {
    batchTimer = null
    onEvent({ batched: true })
  }

  const token = encodeURIComponent(localStorage.getItem('token') || '')
  const es = new EventSource(API_BASE_URL + '/dashboard/stream?token=' + token)

  es.addEventListener('case_update', (e) => {
    if (cancelled) return
    try {
      JSON.parse(e.data) // 校验帧
      if (!batchTimer) batchTimer = setTimeout(emit, 500)
    } catch { /* 忽略坏帧 */ }
  })

  // 401（token 失效）时停止重连，跳登录
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && !cancelled) {
      localStorage.removeItem('token')
      location.hash = '#/login'
    }
  }

  return () => {
    cancelled = true
    clearTimeout(batchTimer)
    es.close()
  }
}

export default api
