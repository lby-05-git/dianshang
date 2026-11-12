<template>
  <div class="gov-page">
    <!-- 顶栏 -->
    <div class="gov-header">
      <div class="header-left">
        <h2>🛡️ 评测治理</h2>
        <span class="header-badge" :class="online ? 'badge-online' : 'badge-offline'">
          {{ online ? '◉ 数据在线' : '◯ 数据离线' }}
        </span>
      </div>
      <div class="header-right">
        <span class="meta-item">只读证据页 · 漂移/预算只告警 · 门禁绝不改退款决策</span>
        <button class="btn gray" style="padding: 4px 12px; font-size: 12px" @click="refreshAll">🔄 刷新</button>
        <a class="back-link" href="#/eval-center">← 评测中心</a>
      </div>
    </div>

    <!-- 标签页：门禁 / 漂移 / 预算 / 告警 / 能力契约 -->
    <div class="tab-bar">
      <button v-for="t in tabs" :key="t.key" class="tab" :class="{ active: curTab === t.key }"
              @click="switchTab(t.key)">
        {{ t.icon }} {{ t.label }}
      </button>
    </div>

    <!-- 门禁（M19 发布门禁证据） -->
    <section v-if="curTab === 'gate'" class="section">
      <div class="section-head">
        <h3>🚥 发布门禁（基线批准 · 候选判定 · 证据留痕）</h3>
      </div>

      <div class="gate-panel">
        <div class="gate-col">
          <h4>① 选择数据集</h4>
          <select v-model="gateSuite" @change="loadBaseline">
            <option v-for="s in suites" :key="s.id" :value="s.id">
              {{ s.name }} (v{{ s.version }} · {{ s.case_count }} 条)
            </option>
          </select>
          <div class="baseline-card" v-if="baseline">
            <p class="muted small">当前发布基线 <span class="tag green">APPROVED</span></p>
            <p class="mono small">{{ short(baseline.run_id, 24) }}</p>
            <p class="muted small">数据集校验和 <span class="mono">{{ short(baseline.dataset_checksum, 14) }}</span></p>
            <p class="muted small">批准人 <span class="tag blue">{{ short(baseline.approved_by, 10) }}</span>
              · {{ fmtTime(baseline.approved_at) }}</p>
            <p v-if="baseline.note" class="muted small reason">备注：{{ baseline.note }}</p>
          </div>
          <div v-else class="baseline-card muted"><p class="small">该数据集尚未批准基线（切换基线只指向证据链 Run）</p></div>
        </div>

        <div class="gate-col">
          <h4>② 选择候选 Run</h4>
          <select v-model="gateRun">
            <option value="">— 选择已完成候选 —</option>
            <option v-for="r in completedRuns" :key="r.run_id" :value="r.run_id">
              {{ short(r.run_id, 20) }} · v{{ r.suite_version }} · {{ rateText(r) }}
            </option>
          </select>
          <label class="inline-label">门禁层级 tier</label>
          <select v-model="gateTier">
            <option value="smoke">smoke</option>
            <option value="core">core</option>
            <option value="full">full</option>
          </select>
          <div class="actions" style="margin-top: 10px">
            <button class="btn" :disabled="!gateSuite || !gateRun || evaluating" @click="doGate">
              {{ evaluating ? '判定中…' : '⚖️ 执行门禁判定' }}
            </button>
            <button class="btn green" :disabled="!gateSuite || !gateRun || gateDecBack" @click="approveBaseline">
              ✓ 批准为基线
            </button>
          </div>
          <div v-if="gateDecBack" class="gate-result">
            <p>判定：
              <span class="tag" :class="gateDecBack.passed ? 'green' : 'red'">{{ gateDecBack.decision }}</span>
            </p>
            <template v-if="gateDecBack.fund_hits?.length">
              <p class="error-text">⛔ 资金红线：{{ gateDecBack.fund_hits.map(f => f.case_id + '/' + f.decision).join(', ') }}</p>
            </template>
            <p v-for="(r, i) in gateDecBack.reason" :key="i" class="small muted">{{ r }}</p>
            <p v-if="gateDecBack.regressed?.length" class="error-text">回归用例：{{ gateDecBack.regressed.join(', ') }}</p>
            <p class="small muted">基线 {{ short(gateDecBack.baseline_run_id, 20) }} · measured={{ gateDecBack.measured }}</p>
          </div>
        </div>
      </div>

      <h4 style="margin: 18px 0 8px">门禁判定历史（证据）</h4>
      <div v-if="gateLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>时间</th><th>Gate ID</th><th>候选 Run</th><th>基线 Run</th><th>Tier</th><th>判定</th><th>结果</th><th>原因</th><th>判定人</th></tr>
        </thead>
        <tbody>
          <tr v-if="!gateResults.length"><td colspan="9" class="muted center">尚无门禁判定记录</td></tr>
          <tr v-for="g in gateResults" :key="g.id">
            <td class="small muted">{{ fmtTime(g.created_at) }}</td>
            <td class="mono small">{{ short(g.gate_id, 20) }}</td>
            <td class="mono small">{{ short(g.run_id, 18) }}</td>
            <td class="mono small">{{ short(g.baseline_run_id || '—', 18) }}</td>
            <td><span class="tag gray">{{ g.tier }}</span></td>
            <td><span class="tag" :class="g.passed ? 'green' : 'red'">{{ g.decision }}</span></td>
            <td><span class="tag" :class="g.passed ? 'green' : 'gray'">{{ g.passed ? '通过' : '阻断' }}</span></td>
            <td class="muted small reason">{{ (g.detail?.reason || []).join('；') }}</td>
            <td class="small muted">{{ short(g.created_by || '—', 10) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 漂移（M20） -->
    <section v-if="curTab === 'drift'" class="section">
      <div class="section-head">
        <h3>📈 漂移监测（错误率 / P95 / 通过率 vs 历史窗口基线）</h3>
        <div class="head-actions">
          <select v-model="driftSuite" style="width: auto" @change="loadDriftSummary">
            <option v-for="s in suites" :key="s.id" :value="s.id">{{ s.name }} (v{{ s.version }})</option>
          </select>
        </div>
      </div>

      <div v-if="driftSummary" class="report-kpis">
        <div v-if="!Object.keys(driftSummary.metrics || {}).length" class="empty small">
          该数据集暂无漂移样本（首轮 run 只记录基线，不判漂移）
        </div>
        <template v-else>
          <div v-for="(m, k) in driftSummary.metrics" :key="k" class="kpi-mini">
            <div class="kpi-mini-num" :class="m.drifted ? 'red' : 'green'">{{ short(m.value, 6) }}</div>
            <div class="kpi-mini-label">{{ k }} {{ m.drifted ? '⚠️漂移' : '正常' }}</div>
            <div class="kpi-mini-sub">基线 {{ short(m.baseline, 6) }}</div>
          </div>
        </template>
        <div class="kpi-mini">
          <div class="kpi-mini-num" style="font-size:14px">{{ fmtTime(driftSummary.latest_at) }}</div>
          <div class="kpi-mini-label">最新样本</div>
        </div>
      </div>

      <h4 style="margin: 18px 0 8px">漂移指标历史（最近 50 条）</h4>
      <div v-if="driftLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>时间</th><th>Run</th><th>指标</th><th>值</th><th>基线</th><th>阈值</th><th>漂移</th></tr>
        </thead>
        <tbody>
          <tr v-if="!driftItems.length"><td colspan="7" class="muted center">暂无漂移记录</td></tr>
          <tr v-for="d in driftItems" :key="d.id">
            <td class="small muted">{{ fmtTime(d.created_at) }}</td>
            <td class="mono small">{{ short(d.run_id, 16) }}</td>
            <td><span class="tag gray">{{ d.metric_key }}</span></td>
            <td class="mono">{{ d.value }}</td>
            <td class="mono muted small">{{ d.baseline }}</td>
            <td class="mono small muted">{{ d.threshold }}</td>
            <td><span class="tag" :class="d.drifted ? 'red' : 'green'">{{ d.drifted ? '漂移' : '正常' }}</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 预算（M20） -->
    <section v-if="curTab === 'budget'" class="section">
      <div class="section-head">
        <h3>💰 成本预算（dimension × dim_key × period · 超支只告警）</h3>
      </div>

      <div class="budget-form card">
        <select v-model="budgetForm.dimension" @change="budgetForm.dim_key = ''">
          <option value="env">env（环境）</option>
          <option value="model">model（模型）</option>
          <option value="prompt">prompt（Prompt）</option>
          <option value="agent">agent（Agent）</option>
          <option value="task_type">task_type</option>
          <option value="run">run</option>
        </select>
        <input v-model="budgetForm.dim_key" placeholder="dim_key（如 claude-opus-5 / sentiment）" />
        <select v-model="budgetForm.period">
          <option value="daily">daily</option>
          <option value="weekly">weekly</option>
          <option value="monthly">monthly</option>
        </select>
        <input v-model.number="budgetForm.limit_amount" type="number" min="0" step="0.01" placeholder="金额上限 ¥" />
        <input v-model.number="budgetForm.limit_tokens" type="number" min="0" placeholder="Token 上限" />
        <label class="check"><input type="checkbox" v-model="budgetForm.enabled" /> 启用</label>
        <button class="btn green" @click="saveBudget">保存预算</button>
        <button class="btn gray" @click="openCheckBudget">⚠️ 手动触发检查</button>
      </div>
      <div v-if="budgetErr" class="error-text">{{ budgetErr }}</div>

      <h4 style="margin: 18px 0 8px">预算设置（{{ budgets.length }}）</h4>
      <div v-if="budgetLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>维度</th><th>Key</th><th>周期</th><th>金额上限</th><th>Token 上限</th><th>启用</th><th>负责人</th></tr>
        </thead>
        <tbody>
          <tr v-if="!budgets.length"><td colspan="7" class="muted center">暂无预算设置</td></tr>
          <tr v-for="b in budgets" :key="b.id">
            <td><span class="tag blue">{{ b.dimension }}</span></td>
            <td class="mono small">{{ b.dim_key }}</td>
            <td><span class="tag gray">{{ b.period }}</span></td>
            <td>¥{{ b.limit_amount }}</td>
            <td class="mono">{{ b.limit_tokens }}</td>
            <td><span class="tag" :class="b.enabled ? 'green' : 'gray'">{{ b.enabled ? '启用' : '停用' }}</span></td>
            <td class="small muted">{{ short(b.owner_id || '—', 10) }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 手动预算检查弹窗 -->
      <div v-if="checkOpen" class="modal-mask" @click.self="checkOpen = false">
        <div class="card modal">
          <h3>⚠️ 手动触发预算检查（只告警，绝不自动中断）</h3>
          <label>维度</label>
          <select v-model="checkForm.dimension">
            <option v-for="d in ['env', 'model', 'prompt', 'agent', 'task_type', 'run']" :key="d" :value="d">{{ d }}</option>
          </select>
          <label>dim_key</label>
          <input v-model="checkForm.dim_key" placeholder="如 claude-opus-5" />
          <label>周期</label>
          <select v-model="checkForm.period">
            <option value="daily">daily</option>
            <option value="weekly">weekly</option>
            <option value="monthly">monthly</option>
          </select>
          <label>当前用量金额 ¥</label>
          <input v-model.number="checkForm.amount" type="number" min="0" step="0.01" />
          <label>当前用量 Tokens</label>
          <input v-model.number="checkForm.tokens" type="number" min="0" />
          <div class="modal-actions">
            <button class="btn" @click="doCheckBudget">检查</button>
            <button class="btn gray" @click="checkOpen = false">取消</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 告警（M20） -->
    <section v-if="curTab === 'alerts'" class="section">
      <div class="section-head">
        <h3>🚨 告警（漂移 / 预算超支 · 只告警 · 人工确认）</h3>
        <div class="head-actions">
          <label class="check"><input type="checkbox" v-model="unackedOnly" @change="loadAlerts" /> 仅未确认</label>
          <button class="btn gray" @click="loadAlerts">🔄 刷新</button>
        </div>
      </div>
      <div v-if="alertLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>时间</th><th>类型</th><th>级别</th><th>指标</th><th>Run</th><th>消息</th><th>状态</th><th>确认人</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!alerts.length"><td colspan="9" class="muted center">{{ unackedOnly ? '没有未确认的告警' : '暂无告警' }}</td></tr>
          <tr v-for="a in alerts" :key="a.id" :class="{ 'alert-row': !a.acked }">
            <td class="small muted">{{ fmtTime(a.created_at) }}</td>
            <td><span class="tag" :class="a.alert_type === 'DRIFT' ? 'yellow' : 'blue'">{{ a.alert_type }}</span></td>
            <td><span class="tag" :class="a.severity === 'critical' ? 'red' : 'yellow'">{{ a.severity }}</span></td>
            <td class="mono small">{{ a.metric_key }}</td>
            <td class="mono small">{{ short(a.run_id || '—', 14) }}</td>
            <td class="muted small reason">{{ a.message }}</td>
            <td><span class="tag" :class="a.acked ? 'gray' : 'red'">{{ a.acked ? '已确认' : '待确认' }}</span></td>
            <td class="small muted">{{ short(a.acked_by || '—', 10) }}</td>
            <td class="actions">
              <button class="btn green small-btn" :disabled="a.acked" @click="ackAlert(a)">{{ a.acked ? '已确认' : '✓ 确认' }}</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 能力契约（M21） -->
    <section v-if="curTab === 'caps'" class="section">
      <div class="section-head">
        <h3>🧩 能力契约（插件注册 · skill 驱动导航 · 未实现不伪 pass）</h3>
        <div class="head-actions">
          <button class="btn gray" @click="loadCapabilities">🔄 刷新</button>
        </div>
      </div>
      <div v-if="capsLoading" class="empty"><div class="spinner"></div></div>
      <template v-else>
        <p class="muted small">核心能力（数据集声明）：<span v-for="c in caps.implemented" :key="c" class="tag green" style="margin-right:6px">{{ c }}</span></p>

        <h4 style="margin: 16px 0 8px">能力注册表</h4>
        <table class="table">
          <thead>
            <tr><th>能力</th><th>可用性</th><th>已实现</th><th>Modality</th><th>Streaming</th></tr>
          </thead>
          <tbody>
            <tr v-for="(m, name) in caps.capabilities" :key="name">
              <td><span class="tag" :class="m.implemented ? 'green' : 'gray'">{{ name }}</span></td>
              <td><span class="tag" :class="m.availability === 'available' ? 'green' : (m.availability === 'unavailable' ? 'gray' : 'yellow')">{{ m.availability }}</span></td>
              <td>{{ m.implemented ? '✔' : '✘' }}</td>
              <td class="muted small">{{ m.modality || '—' }}</td>
              <td class="muted small">{{ m.streaming ? '是' : '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4 style="margin: 16px 0 8px">未实现 Metric（如实标记未测，绝不判通过）</h4>
        <p v-if="!caps.unimplemented_metrics.length" class="muted small">无已声明但未测试的 Metric</p>
        <span v-for="m in caps.unimplemented_metrics" :key="m" class="tag gray" style="margin-right: 6px">{{ m }}</span>
      </template>
    </section>

    <div v-if="toast" class="toast">✅ {{ toast }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import {
  evalAckAlert, evalApproveBaseline, evalCheckBudget, evalDriftSummary, evalEvaluateGate,
  evalGetBaseline, evalGetCapabilities, evalListAlerts, evalListBudgets, evalListDrift,
  evalListGateResults, evalListRuns, evalListSuites, evalUpsertBudget,
} from '../api'

const tabs = [
  { key: 'gate', icon: '🚥', label: '门禁' },
  { key: 'drift', icon: '📈', label: '漂移' },
  { key: 'budget', icon: '💰', label: '预算' },
  { key: 'alerts', icon: '🚨', label: '告警' },
  { key: 'caps', icon: '🧩', label: '能力' },
]
const curTab = ref('gate')
const online = ref(true)
const toast = ref('')
let toastTimer = null

function showToast(msg) {
  toast.value = msg
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toast.value = ''), 2600)
}
function errMsg(e) {
  return (e.response?.data?.detail) || (e.response?.data?.message) || e.message || '请求失败'
}

// 数据集 / Runs（门禁候选）
const suites = ref([])
const completedRuns = ref([])

async function loadSuites() {
  try {
    const res = await evalListSuites()
    suites.value = res.data.items || []
  } catch (e) { online.value = false }
}
async function loadRuns() {
  try {
    const res = await evalListRuns()
    completedRuns.value = (res.data.items || []).filter(r => r.status === 'COMPLETED')
  } catch (e) { online.value = false }
}

// ---- 门禁 ----
const gateSuite = ref('')
const gateRun = ref('')
const gateTier = ref('full')
const baseline = ref(null)
const gateResults = ref([])
const gateLoading = ref(true)
const evaluating = ref(false)
const gateDecBack = ref(null)

async function loadBaseline() {
  baseline.value = null
  gateDecBack.value = null
  if (!gateSuite.value) return
  try {
    const res = await evalGetBaseline(gateSuite.value)
    baseline.value = res.data.item || null
  } catch (e) { /* 404 = 无基线，保持 null */ }
}

async function loadGateResults() {
  gateLoading.value = true
  try {
    const res = await evalListGateResults()
    gateResults.value = res.data.items || []
  } catch (e) { online.value = false } finally { gateLoading.value = false }
}

async function doGate() {
  if (!gateSuite.value || !gateRun.value) return
  evaluating.value = true
  try {
    const res = await evalEvaluateGate(gateRun.value, gateSuite.value, gateTier.value)
    gateDecBack.value = res.data.item
    showToast(`门禁判定：${res.data.item.decision}`)
    await loadGateResults()
  } catch (e) { alert('门禁判定失败：' + errMsg(e)) }
  finally { evaluating.value = false }
}

async function approveBaseline() {
  if (!gateSuite.value || !gateRun.value) return
  if (!confirm(`批准 ${gateRun.value} 为「${(suites.value.find(s => s.id === gateSuite.value) || {}).name || gateSuite.value}」的发布基线？`)) return
  try {
    const res = await evalApproveBaseline(gateSuite.value, gateRun.value)
    baseline.value = res.data.item
    showToast('基线已批准（approver/approved_at 留痕）')
  } catch (e) { alert('批准失败：' + errMsg(e)) }
}

// ---- 漂移 ----
const driftSuite = ref('')
const driftSummary = ref(null)
const driftItems = ref([])
const driftLoading = ref(true)

async function loadDriftSummary() {
  driftSummary.value = null
  if (!driftSuite.value) return
  try {
    const res = await evalDriftSummary(driftSuite.value)
    driftSummary.value = res.data.item || null
  } catch (e) { online.value = false }
}

async function loadDrift() {
  driftLoading.value = true
  try {
    const res = await evalListDrift()
    driftItems.value = res.data.items || []
  } catch (e) { online.value = false } finally { driftLoading.value = false }
}

// ---- 预算 ----
const budgets = ref([])
const budgetLoading = ref(true)
const budgetForm = ref({ dimension: 'model', dim_key: '', period: 'daily', limit_amount: 100, limit_tokens: 1000000, enabled: true })
const budgetErr = ref('')
const checkOpen = ref(false)
const checkForm = ref({ dimension: 'model', dim_key: '', period: 'daily', amount: 0, tokens: 0 })

async function loadBudgets() {
  budgetLoading.value = true
  try {
    const res = await evalListBudgets()
    budgets.value = res.data.items || []
  } catch (e) { online.value = false } finally { budgetLoading.value = false }
}

async function saveBudget() {
  budgetErr.value = ''
  if (!budgetForm.value.dim_key.trim()) { budgetErr.value = 'dim_key 必填'; return }
  try {
    await evalUpsertBudget(budgetForm.value)
    await loadBudgets()
    showToast('预算已保存（超支只告警）')
    budgetForm.value.dim_key = ''
  } catch (e) { budgetErr.value = '保存失败：' + errMsg(e) }
}

function openCheckBudget() {
  checkForm.value = { dimension: 'model', dim_key: '', period: 'daily', amount: 0, tokens: 0 }
  checkOpen.value = true
}

async function doCheckBudget() {
  try {
    const res = await evalCheckBudget(checkForm.value)
    checkOpen.value = false
    const n = (res.data.items || []).length
    showToast(n ? `已产生 ${n} 条预算告警` : '未超预支，无新告警')
    await Promise.all([loadBudgets(), loadAlerts()])
  } catch (e) { alert('检查失败：' + errMsg(e)) }
}

// ---- 告警 ----
const alerts = ref([])
const alertLoading = ref(true)
const unackedOnly = ref(false)

async function loadAlerts() {
  alertLoading.value = true
  try {
    const res = await evalListAlerts(unackedOnly.value)
    alerts.value = res.data.items || []
  } catch (e) { online.value = false } finally { alertLoading.value = false }
}

async function ackAlert(a) {
  try {
    const res = await evalAckAlert(a.id)
    showToast(`告警已确认（${res.data.item?.acked_by || ''}）`)
    await loadAlerts()
  } catch (e) { alert('确认失败：' + errMsg(e)) }
}

// ---- 能力契约 ----
const caps = ref({ implemented: [], capabilities: {}, unimplemented: [], unimplemented_metrics: [], metrics: {} })
const capsLoading = ref(true)

async function loadCapabilities() {
  capsLoading.value = true
  try {
    const res = await evalGetCapabilities()
    caps.value = res.data || {}
  } catch (e) { online.value = false } finally { capsLoading.value = false }
}

// ---- 工具 ----
function short(s, n) { return s && s.length > n ? s.slice(0, n) + '…' : s }
function fmtTime(t) { return t ? String(t).replace('T', ' ').slice(0, 16) : '—' }
function rateText(r) {
  const pr = r.summary?.pass_rate
  return pr != null ? (pr * 100).toFixed(0) + '%' : '—'
}
function switchTab(k) { curTab.value = k }

function refreshAll() {
  online.value = true
  if (curTab.value === 'gate') { loadGateResults(); loadBaseline(); loadSuites(); loadRuns() }
  else if (curTab.value === 'drift') { loadDrift(); loadDriftSummary(); loadSuites() }
  else if (curTab.value === 'budget') { loadBudgets() }
  else if (curTab.value === 'alerts') { loadAlerts() }
  else if (curTab.value === 'caps') { loadCapabilities() }
}

onMounted(async () => {
  await Promise.all([loadSuites(), loadRuns(), loadGateResults(), loadDrift(), loadBudgets(), loadAlerts(), loadCapabilities()])
  if (suites.value.length) {
    gateSuite.value = suites.value[0].id
    driftSuite.value = suites.value[0].id
    await Promise.all([loadBaseline(), loadDriftSummary()])
  }
})
</script>

<style scoped>
.gov-page { display: flex; flex-direction: column; gap: 14px; }

.gov-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 12px 18px; box-shadow: var(--shadow-sm);
}
.header-left { display: flex; align-items: center; gap: 10px; }
.gov-header h2 { font-size: 17px; letter-spacing: 0.5px; margin: 0; }
.header-badge { font-size: 11px; padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge-online { background: rgba(5, 150, 105, 0.12); color: var(--green); border: 1px solid rgba(5, 150, 105, 0.25); }
.badge-offline { background: rgba(220, 38, 38, 0.12); color: var(--red); border: 1px solid rgba(220, 38, 38, 0.25); }
.header-right { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-left: auto; align-items: center; }
.back-link { font-size: 12px; color: var(--blue); text-decoration: none; padding: 4px 10px; border: 1px solid rgba(96,165,250,0.3); border-radius: 6px; }
.back-link:hover { background: rgba(96,165,250,0.1); }

.tab-bar { display: flex; gap: 8px; flex-wrap: wrap; }
.tab {
  border: 1px solid var(--border); background: var(--panel); color: var(--muted);
  padding: 8px 16px; border-radius: 10px; font-size: 13px; cursor: pointer; transition: all 0.15s;
}
.tab.active { background: var(--grad-blue); color: #fff; border-color: transparent; box-shadow: 0 3px 10px rgba(59,130,246,0.35); }

.section { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; box-shadow: var(--shadow-sm); }
.section-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.section-head h3 { font-size: 14.5px; letter-spacing: 0.3px; margin: 0; }
.head-actions { display: flex; gap: 8px; align-items: center; }

.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table thead th { text-align: left; font-size: 11.5px; color: var(--muted); border-bottom: 1px solid var(--border); padding: 7px 8px; white-space: nowrap; }
.table tbody td { padding: 8px; border-bottom: 1px solid var(--border-light); vertical-align: middle; }
.table.small { font-size: 12.5px; }
.actions { display: flex; gap: 6px; white-space: nowrap; }
.small-btn { padding: 3px 10px; font-size: 12px; }
.reason { max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.center { text-align: center; }
.mono { font-family: 'Cascadia Code', Consolas, monospace; }
.small { font-size: 12px; }
.muted { color: var(--muted); }
.empty { text-align: center; padding: 40px 16px; }
.spinner { width: 30px; height: 30px; margin: 0 auto; border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.gate-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.gate-col { display: flex; flex-direction: column; gap: 8px; }
.gate-col h4 { margin: 0; font-size: 13.5px; }
.baseline-card { background: var(--panel2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
.gate-result { background: rgba(251, 191, 36, 0.08); border: 1px solid rgba(251, 191, 36, 0.3); border-radius: 10px; padding: 10px 12px; margin-top: 8px; }
.inline-label { margin: 6px 0 0; }

.budget-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; padding: 12px; }
.budget-form select, .budget-form input { width: auto; min-width: 140px; }
.budget-form .check { align-items: center; }
.budget-form .btn { white-space: nowrap; }

.report-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin: 6px 0 14px; }
.kpi-mini { background: var(--panel2); border-radius: 10px; padding: 10px; text-align: center; }
.kpi-mini-num { font-size: 20px; font-weight: 750; }
.kpi-mini-num.green { color: var(--green); }
.kpi-mini-num.red { color: var(--red); }
.kpi-mini-num.yellow { color: var(--yellow); }
.kpi-mini-label { font-size: 11px; color: var(--muted); margin-top: 2px; }
.kpi-mini-sub { font-size: 10.5px; color: var(--muted); margin-top: 2px; }

.alert-row { background: rgba(251, 191, 36, 0.05); }

.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { width: 460px; max-width: calc(100vw - 32px); max-height: 84vh; overflow: auto; }
.modal-actions { display: flex; gap: 10px; margin-top: 16px; }
.check { display: flex; align-items: center; gap: 8px; }
.check input { width: auto; }

@media (max-width: 900px) {
  .gate-panel { grid-template-columns: 1fr; }
  .header-right { width: 100%; margin-left: 0; flex-wrap: wrap; }
}
</style>