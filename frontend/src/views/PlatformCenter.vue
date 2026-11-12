<template>
  <div class="page-stack">
    <PageHeader
      eyebrow="RELEASE EVIDENCE & RECONCILIATION"
      title="发布与一致性治理"
      description="统一查看订单核验、业务决策、退款回执的对账差异，以及Agent、Prompt、模型、工具和策略的版本审批记录。"
    >
      <template #actions>
        <button class="btn secondary" :disabled="loading" @click="refreshAll"><AppIcon name="refresh" />刷新</button>
        <button class="btn" :disabled="running" @click="runReconciliation"><AppIcon name="reconcile" />{{ running ? '对账中…' : '立即对账' }}</button>
      </template>
    </PageHeader>

    <div class="metric-grid">
      <div class="metric-card">
        <div class="metric-top"><span>核验失败率</span><span class="metric-icon orange"><AppIcon name="warning" /></span></div>
        <div class="metric-value">{{ pct(metrics.verify_fail_rate) }}</div>
        <div class="metric-foot">{{ metrics.verify_total ?? 0 }} 次订单核验</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>退款失败率</span><span class="metric-icon" :class="(metrics.refund_fail_rate || 0) > 0 ? 'red' : 'green'"><AppIcon name="activity" /></span></div>
        <div class="metric-value">{{ pct(metrics.refund_fail_rate) }}</div>
        <div class="metric-foot">{{ metrics.refund_ok ?? 0 }} 笔成功 · {{ metrics.refund_retries ?? 0 }} 次重试</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>高危差异</span><span class="metric-icon red"><AppIcon name="reconcile" /></span></div>
        <div class="metric-value">{{ metrics.recon_high_events ?? 0 }}</div>
        <div class="metric-foot">不一致率 {{ pct(metrics.inconsistency_rate) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>待处置告警</span><span class="metric-icon" :class="overview.unresolved_alerts ? 'red' : 'green'"><AppIcon name="shield" /></span></div>
        <div class="metric-value">{{ overview.unresolved_alerts ?? 0 }}</div>
        <div class="metric-foot">{{ overview.window_minutes || 60 }}分钟观测窗口</div>
      </div>
    </div>

    <div v-if="error" class="notice danger"><AppIcon name="warning" />{{ error }}</div>

    <div class="segmented platform-tabs">
      <button :class="{ active: tab === 'recon' }" @click="tab = 'recon'">一致性对账</button>
      <button :class="{ active: tab === 'versions' }" @click="tab = 'versions'">统一版本中心</button>
    </div>

    <section v-if="tab === 'recon'" class="card">
      <div class="card-header">
        <div><div class="card-title">差异与异常告警</div><div class="card-description">对账只生成证据和告警，不自动篡改业务事实或外部支付回执。</div></div>
        <div class="card-toolbar">
          <select v-model="reconFilter.severity" class="compact-select" @change="loadDiffs">
            <option value="">全部级别</option><option value="CRITICAL">CRITICAL</option><option value="HIGH">HIGH</option><option value="WARNING">WARNING</option><option value="INFO">INFO</option>
          </select>
          <label class="check-filter"><input v-model="reconFilter.unresolved_only" type="checkbox" @change="loadDiffs" />仅未处置</label>
        </div>
      </div>
      <div v-if="loading" class="empty-state"><div class="spinner"></div></div>
      <div v-else-if="!diffs.length" class="empty-state"><div><AppIcon name="check" /><strong>当前没有一致性差异</strong><p>订单、决策和退款回执口径一致。</p></div></div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>严重级</th><th>差异类别</th><th>案件</th><th>字段</th><th>期望值</th><th>实际值</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in diffs" :key="item.id">
            <td><span class="tag" :class="severityClass(item.severity)">{{ item.severity }}</span></td>
            <td>{{ item.category }}</td>
            <td><a :href="'#/cases/' + item.case_id" class="mono">{{ shortId(item.case_id) }}</a></td>
            <td class="mono">{{ item.field }}</td>
            <td class="value-cell">{{ displayValue(item.expected) }}</td>
            <td class="value-cell">{{ displayValue(item.actual) }}</td>
            <td><span class="tag" :class="item.resolved ? 'green' : 'yellow'">{{ item.resolved ? '已处置' : '待处置' }}</span></td>
            <td><button class="btn small secondary" :disabled="item.resolved || resolving === item.id" @click="resolveDiff(item)">{{ resolving === item.id ? '处理中…' : '确认处置' }}</button></td>
          </tr></tbody>
        </table>
      </div>
    </section>

    <section v-else class="card">
      <div class="card-header">
        <div><div class="card-title">版本注册与审批</div><div class="card-description">所有关键资产保留指纹、变更说明、批准人和回滚关联，生产规则不会自动生效。</div></div>
        <div class="card-toolbar">
          <select v-model="versionFilter.entity_type" class="domain-select" @change="loadVersions">
            <option value="">全部资产类型</option><option v-for="item in domains" :key="item" :value="item">{{ domainText(item) }}</option>
          </select>
          <select v-model="versionFilter.status" class="compact-select" @change="loadVersions">
            <option value="">全部状态</option><option value="DRAFT">DRAFT</option><option value="CANDIDATE">CANDIDATE</option><option value="APPROVED">APPROVED</option><option value="REJECTED">REJECTED</option><option value="ROLLED_BACK">ROLLED_BACK</option>
          </select>
        </div>
      </div>
      <div v-if="loadingVersions" class="empty-state"><div class="spinner"></div></div>
      <div v-else-if="!versions.length" class="empty-state"><div><AppIcon name="database" /><strong>暂无版本登记</strong><p>版本记录将由发布流程或治理负责人登记。</p></div></div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>资产类型</th><th>资产名称</th><th>版本</th><th>状态</th><th>变更说明</th><th>内容指纹</th><th>创建时间</th><th>审批</th></tr></thead>
          <tbody><tr v-for="item in versions" :key="item.id">
            <td><span class="tag blue">{{ domainText(item.entity_type) }}</span></td>
            <td><strong>{{ item.entity_name }}</strong></td><td class="mono">{{ item.version }}</td>
            <td><span class="tag" :class="versionClass(item.status)">{{ item.status }}</span></td>
            <td class="changelog" :title="item.changelog">{{ item.changelog || '—' }}</td>
            <td class="mono fingerprint" :title="item.content_sha256">{{ shortHash(item.content_sha256) }}</td>
            <td class="muted">{{ fmtTime(item.created_at) }}</td>
            <td><div v-if="canGovern && ['DRAFT','CANDIDATE'].includes(item.status)" class="row-actions">
              <button class="btn small green" @click="approveVersion(item)">批准</button>
              <button class="btn small red" @click="rejectVersion(item)">驳回</button>
            </div><span v-else class="muted">—</span></td>
          </tr></tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import {
  reconGetMetrics,
  reconGetOverview,
  reconListDiffs,
  reconResolveAlert,
  reconRun,
  versionApprove,
  versionList,
  versionListDomains,
  versionReject,
} from '../api'

const tab = ref('recon')
const metrics = ref({})
const overview = ref({})
const diffs = ref([])
const domains = ref([])
const versions = ref([])
const error = ref('')
const loading = ref(false)
const loadingVersions = ref(false)
const running = ref(false)
const resolving = ref('')
const reconFilter = reactive({ severity: '', unresolved_only: true })
const versionFilter = reactive({ entity_type: '', status: '' })
const canGovern = computed(() => localStorage.getItem('role') === 'leader')

function messageOf(e) {
  const detail = e.response?.data?.detail || e.response?.data?.message
  return typeof detail === 'string' ? detail : detail?.message || '请求失败，请稍后重试'
}

async function loadOverview() {
  const [overviewRes, metricRes] = await Promise.all([reconGetOverview(), reconGetMetrics()])
  overview.value = overviewRes.data.item || {}
  metrics.value = metricRes.data.item || {}
}

async function loadDiffs() {
  loading.value = true
  try {
    const { data } = await reconListDiffs({
      severity: reconFilter.severity || undefined,
      unresolved_only: reconFilter.unresolved_only,
      limit: 100,
    })
    diffs.value = data.items || []
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

async function loadVersions() {
  loadingVersions.value = true
  try {
    const { data } = await versionList({
      entity_type: versionFilter.entity_type || undefined,
      status: versionFilter.status || undefined,
      limit: 100,
    })
    versions.value = data.items || []
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loadingVersions.value = false
  }
}

async function refreshAll() {
  error.value = ''
  try {
    const domainRes = await versionListDomains()
    domains.value = domainRes.data.domains || []
    await Promise.all([loadOverview(), loadDiffs(), loadVersions()])
  } catch (e) {
    error.value = messageOf(e)
  }
}

async function runReconciliation() {
  if (!window.confirm('确认运行一轮一致性对账？该操作只生成差异证据，不会修改业务或退款状态。')) return
  running.value = true
  try {
    await reconRun(60)
    await Promise.all([loadOverview(), loadDiffs()])
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    running.value = false
  }
}

async function resolveDiff(item) {
  resolving.value = item.id
  try {
    await reconResolveAlert(item.id)
    await Promise.all([loadOverview(), loadDiffs()])
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    resolving.value = ''
  }
}

async function approveVersion(item) {
  if (!window.confirm('确认批准 ' + item.entity_name + ' ' + item.version + '？')) return
  try { await versionApprove(item.id); await loadVersions() } catch (e) { error.value = messageOf(e) }
}
async function rejectVersion(item) {
  if (!window.confirm('确认驳回 ' + item.entity_name + ' ' + item.version + '？')) return
  try { await versionReject(item.id); await loadVersions() } catch (e) { error.value = messageOf(e) }
}

const pct = (value) => value == null ? '未测量' : (Number(value) * 100).toFixed(1) + '%'
const shortId = (value) => value ? String(value).slice(0, 8) : '—'
const shortHash = (value) => value ? String(value).slice(0, 12) : '—'
const fmtTime = (value) => value ? value.replace('T', ' ').slice(0, 19) : '—'
const displayValue = (value) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const severityClass = (value) => ({ CRITICAL: 'red', HIGH: 'red', WARNING: 'yellow', INFO: 'blue' }[value] || 'gray')
const versionClass = (value) => ({ APPROVED: 'green', DRAFT: 'gray', CANDIDATE: 'blue', REJECTED: 'red', ROLLED_BACK: 'yellow' }[value] || 'gray')
const domainText = (value) => ({ agent: 'Agent', prompt: 'Prompt', model: '模型', tool: '工具', policy: '策略', dataset: '数据集', security_policy: '安全策略' }[value] || value)

onMounted(refreshAll)
</script>

<style scoped>
.platform-tabs { align-self: flex-start; }
.compact-select { width: 126px; min-height: 34px; }
.domain-select { width: 155px; min-height: 34px; }
.check-filter { display: flex; align-items: center; gap: 6px; margin: 0; white-space: nowrap; }
.check-filter input { width: 16px; min-height: 16px; }
.value-cell { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: Consolas, monospace; font-size: 12px; }
.changelog { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fingerprint { color: var(--muted); }
.row-actions { display: flex; gap: 6px; }
@media (max-width: 680px) { .card-toolbar { width: 100%; align-items: stretch; flex-direction: column; } .compact-select, .domain-select { width: 100%; } }
</style>
