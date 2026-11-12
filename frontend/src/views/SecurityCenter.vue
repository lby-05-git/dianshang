<template>
  <div class="page-stack">
    <PageHeader
      eyebrow="ZERO TRUST OPERATIONS"
      title="安全运营中心"
      description="统一查看输入安全判定、DLP脱敏证据和安全事件处置状态；安全服务异常时，高风险资金操作始终按Fail-Closed处理。"
    >
      <template #actions>
        <button class="btn secondary" :disabled="loading" @click="loadEvents">
          <AppIcon name="refresh" />刷新事件
        </button>
      </template>
    </PageHeader>

    <div class="metric-grid">
      <div class="metric-card">
        <div class="metric-top"><span>安全事件</span><span class="metric-icon"><AppIcon name="shield" /></span></div>
        <div class="metric-value">{{ events.length }}</div>
        <div class="metric-foot">当前筛选窗口内事件</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>待处置</span><span class="metric-icon orange"><AppIcon name="warning" /></span></div>
        <div class="metric-value">{{ openCount }}</div>
        <div class="metric-foot">OPEN / TRIAGE 状态</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>高危事件</span><span class="metric-icon red"><AppIcon name="activity" /></span></div>
        <div class="metric-value">{{ highRiskCount }}</div>
        <div class="metric-foot">HIGH / CRITICAL 严重级</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>已闭环</span><span class="metric-icon green"><AppIcon name="check" /></span></div>
        <div class="metric-value">{{ closedCount }}</div>
        <div class="metric-foot">已完成审计处置</div>
      </div>
    </div>

    <div class="segmented security-tabs">
      <button :class="{ active: activeTab === 'events' }" @click="activeTab = 'events'">事件工作台</button>
      <button :class="{ active: activeTab === 'critic' }" @click="activeTab = 'critic'">Critic检测</button>
      <button :class="{ active: activeTab === 'dlp' }" @click="activeTab = 'dlp'">DLP脱敏</button>
    </div>

    <section v-if="activeTab === 'events'" class="card">
      <div class="card-header">
        <div>
          <div class="card-title">安全事件队列</div>
          <div class="card-description">处置只改变安全事件自身状态，不修改工单决策或退款状态。</div>
        </div>
        <div class="security-filters">
          <select v-model="filters.disposition" @change="loadEvents">
            <option value="">全部状态</option>
            <option value="OPEN">OPEN</option>
            <option value="TRIAGE">TRIAGE</option>
            <option value="CONFIRMED">CONFIRMED</option>
            <option value="FALSE_POSITIVE">FALSE_POSITIVE</option>
            <option value="CLOSED">CLOSED</option>
          </select>
          <select v-model="filters.severity" @change="loadEvents">
            <option value="">全部级别</option>
            <option value="CRITICAL">CRITICAL</option>
            <option value="HIGH">HIGH</option>
            <option value="MEDIUM">MEDIUM</option>
            <option value="LOW">LOW</option>
          </select>
        </div>
      </div>

      <div v-if="error" class="notice danger"><AppIcon name="warning" />{{ error }}</div>
      <div v-else-if="loading" class="empty-state"><div><div class="spinner"></div><p>正在加载安全证据…</p></div></div>
      <div v-else-if="!events.length" class="empty-state">
        <div><AppIcon name="shield" /><strong>当前没有安全事件</strong><p>调整筛选条件或运行Critic检测后再查看。</p></div>
      </div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>严重级</th><th>事件类型</th><th>关联案件</th><th>处置状态</th><th>证据摘要</th><th>发生时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="item in events" :key="item.id">
              <td><span class="tag" :class="severityClass(item.severity)">{{ item.severity }}</span></td>
              <td class="mono">{{ item.event_type }}</td>
              <td><a v-if="item.case_id" :href="'#/cases/' + item.case_id" class="mono">{{ shortId(item.case_id) }}</a><span v-else class="muted">独立检测</span></td>
              <td><span class="status-chip" :class="dispositionClass(item.disposition)">{{ item.disposition }}</span></td>
              <td class="detail-cell" :title="item.detail">{{ item.detail || '—' }}</td>
              <td class="muted">{{ fmtTime(item.created_at) }}</td>
              <td>
                <div class="row-actions">
                  <button
                    v-for="action in actionsFor(item)"
                    :key="action.value"
                    class="btn small secondary"
                    :disabled="busyId === item.id"
                    @click="dispose(item, action.value)"
                  >{{ action.label }}</button>
                  <span v-if="!actionsFor(item).length" class="muted">已归档</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-else-if="activeTab === 'critic'" class="content-grid">
      <div class="card span-5">
        <div class="card-header">
          <div><div class="card-title">输入安全检测</div><div class="card-description">规则与轻量语义联合识别Prompt注入、越狱和角色伪装。</div></div>
        </div>
        <label>待检测文本</label>
        <textarea v-model="criticForm.text" rows="8" placeholder="输入需要通过安全网关检查的用户内容"></textarea>
        <div class="field-row">
          <div><label>调用角色</label><select v-model="criticForm.role"><option value="customer">客户</option><option value="agent">Agent</option><option value="officer">安全人员</option></select></div>
          <div><label>业务场景</label><select v-model="criticForm.scenario"><option value="refund">退款</option><option value="voice">语音</option><option value="paper">文档</option><option value="sandbox">沙箱</option></select></div>
        </div>
        <label>关联案件ID（可选）</label>
        <input v-model="criticForm.case_id" placeholder="用于关联审计证据" />
        <button class="btn run-btn" :disabled="criticBusy || !criticForm.text.trim()" @click="runCritic">
          <AppIcon name="shield" />{{ criticBusy ? '检测中…' : '运行安全检测' }}
        </button>
        <div v-if="labError" class="error-text">{{ labError }}</div>
      </div>
      <div class="card span-7 result-panel">
        <div class="card-header"><div><div class="card-title">可解释判定结果</div><div class="card-description">风险分、命中规则和脱敏输入均来自真实接口。</div></div></div>
        <div v-if="!criticResult" class="empty-state"><div><AppIcon name="activity" /><strong>等待检测</strong><p>结果不会触发任何业务动作或退款。</p></div></div>
        <template v-else>
          <div class="verdict-row">
            <div><span class="muted">安全结论</span><strong :class="'verdict-' + criticResult.verdict">{{ criticResult.verdict }}</strong></div>
            <div><span class="muted">风险等级</span><strong>{{ criticResult.risk_level }}</strong></div>
            <div><span class="muted">风险分</span><strong>{{ fmtScore(criticResult.risk_score) }}</strong></div>
          </div>
          <div class="result-block"><label>原因说明</label><ul><li v-for="reason in criticResult.reasons || []" :key="reason">{{ reason }}</li><li v-if="!(criticResult.reasons || []).length">未命中高风险原因</li></ul></div>
          <div class="result-block"><label>脱敏后输入</label><pre>{{ criticResult.masked_input || '—' }}</pre></div>
          <div class="result-block"><label>审计标识</label><code>{{ criticResult.decision_id || '未落审计' }}</code></div>
        </template>
      </div>
    </section>

    <section v-else class="content-grid">
      <div class="card span-5">
        <div class="card-header"><div><div class="card-title">敏感信息脱敏</div><div class="card-description">按角色与场景执行NER识别和策略化掩码，原文不落库。</div></div></div>
        <label>原始文本</label>
        <textarea v-model="dlpForm.text" rows="8" placeholder="示例：联系电话13800138000，银行卡号…"></textarea>
        <div class="field-row">
          <div><label>查看角色</label><select v-model="dlpForm.role"><option value="customer">客户</option><option value="agent">Agent</option><option value="officer">安全人员</option></select></div>
          <div><label>使用场景</label><select v-model="dlpForm.scenario"><option value="refund">退款</option><option value="voice">语音</option><option value="paper">文档</option><option value="sandbox">沙箱</option></select></div>
        </div>
        <button class="btn run-btn" :disabled="dlpBusy || !dlpForm.text.trim()" @click="runDlp">
          <AppIcon name="shield" />{{ dlpBusy ? '处理中…' : '执行安全脱敏' }}
        </button>
      </div>
      <div class="card span-7 result-panel">
        <div class="card-header"><div><div class="card-title">脱敏结果</div><div class="card-description">非NONE敏感实体不应在输出中保留明文。</div></div></div>
        <div v-if="!dlpResult" class="empty-state"><div><AppIcon name="shield" /><strong>等待处理</strong><p>输入内容仅用于本次即时脱敏。</p></div></div>
        <template v-else>
          <div class="result-block"><label>安全文本</label><pre>{{ dlpResult.masked_text }}</pre></div>
          <div class="entity-list">
            <span v-for="(entity, index) in dlpResult.entities || []" :key="index" class="tag purple">{{ entity.entity_type || entity.type }} · {{ entity.action || 'MASK' }}</span>
            <span v-if="!(dlpResult.entities || []).length" class="tag green">未识别到敏感实体</span>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import {
  securityCritic,
  securityDisposeEvent,
  securityListEvents,
  securityMask,
} from '../api'

const activeTab = ref('events')
const events = ref([])
const loading = ref(false)
const error = ref('')
const busyId = ref('')
const labError = ref('')
const criticBusy = ref(false)
const dlpBusy = ref(false)
const criticResult = ref(null)
const dlpResult = ref(null)
const filters = reactive({ disposition: '', severity: '' })
const criticForm = reactive({ text: '', case_id: '', role: 'agent', scenario: 'refund', source: 'critic' })
const dlpForm = reactive({ text: '', role: 'agent', scenario: 'refund' })

const openCount = computed(() => events.value.filter((x) => ['OPEN', 'TRIAGE'].includes(x.disposition)).length)
const highRiskCount = computed(() => events.value.filter((x) => ['HIGH', 'CRITICAL'].includes(x.severity)).length)
const closedCount = computed(() => events.value.filter((x) => x.disposition === 'CLOSED').length)

function messageOf(e) {
  const detail = e.response?.data?.detail || e.response?.data?.message
  return typeof detail === 'string' ? detail : detail?.message || '请求失败，请稍后重试'
}

async function loadEvents() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await securityListEvents({
      disposition: filters.disposition || undefined,
      severity: filters.severity || undefined,
      limit: 100,
    })
    events.value = data.items || []
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

function actionsFor(item) {
  if (item.disposition === 'OPEN') return [{ value: 'triage', label: '开始研判' }]
  if (item.disposition === 'TRIAGE') return [{ value: 'confirm', label: '确认风险' }, { value: 'false_positive', label: '标记误报' }]
  if (['CONFIRMED', 'FALSE_POSITIVE'].includes(item.disposition)) return [{ value: 'close', label: '关闭事件' }]
  return []
}

async function dispose(item, action) {
  if (action === 'close' && !window.confirm('确认关闭该安全事件？关闭后审计记录仍会保留。')) return
  busyId.value = item.id
  try {
    await securityDisposeEvent(item.id, action, '前端安全运营工作台处置')
    await loadEvents()
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    busyId.value = ''
  }
}

async function runCritic() {
  criticBusy.value = true
  labError.value = ''
  try {
    const body = { ...criticForm, case_id: criticForm.case_id || null }
    const { data } = await securityCritic(body)
    criticResult.value = data
    if ((data.event_ids || []).length) loadEvents()
  } catch (e) {
    labError.value = messageOf(e)
  } finally {
    criticBusy.value = false
  }
}

async function runDlp() {
  dlpBusy.value = true
  labError.value = ''
  try {
    const { data } = await securityMask({ ...dlpForm })
    dlpResult.value = data
  } catch (e) {
    labError.value = messageOf(e)
  } finally {
    dlpBusy.value = false
  }
}

const shortId = (id) => (id || '').slice(0, 8)
const fmtTime = (value) => value ? value.replace('T', ' ').slice(0, 19) : '—'
const fmtScore = (value) => value == null ? '未测量' : Number(value).toFixed(2)
const severityClass = (value) => ({ CRITICAL: 'red', HIGH: 'red', MEDIUM: 'yellow', LOW: 'blue' }[value] || 'gray')
const dispositionClass = (value) => ({ OPEN: 'red', TRIAGE: 'yellow', CONFIRMED: 'purple', FALSE_POSITIVE: 'blue', CLOSED: 'green' }[value] || 'gray')

onMounted(loadEvents)
</script>

<style scoped>
.security-tabs { align-self: flex-start; }
.security-filters { display: flex; gap: 8px; }
.security-filters select { width: 138px; min-height: 34px; }
.detail-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-actions { display: flex; gap: 6px; min-width: 110px; }
.run-btn { width: 100%; margin-top: 16px; }
.result-panel { min-height: 430px; }
.verdict-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.verdict-row > div { padding: 13px; background: var(--panel2); border-radius: 10px; }
.verdict-row span, .verdict-row strong { display: block; }
.verdict-row strong { margin-top: 4px; font-size: 18px; }
.verdict-BLOCK { color: var(--red); }
.verdict-REVIEW { color: var(--yellow); }
.verdict-ALLOW, .verdict-PASS { color: var(--green); }
.result-block { margin-top: 16px; }
.result-block ul { padding-left: 18px; color: #526176; font-size: 13px; }
.result-block pre { min-height: 72px; padding: 12px; overflow: auto; background: #f7f9fc; border: 1px solid var(--border); border-radius: 9px; white-space: pre-wrap; word-break: break-word; }
.result-block code { display: block; padding: 9px 11px; color: #334155; background: #f7f9fc; border-radius: 8px; font-size: 12px; }
.entity-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
@media (max-width: 680px) {
  .security-filters { width: 100%; flex-direction: column; }
  .security-filters select { width: 100%; }
  .verdict-row { grid-template-columns: 1fr; }
}
</style>
