<template>
  <div class="page-stack">
    <PageHeader
      eyebrow="INTENT RELIABILITY"
      title="意图可靠性中心"
      description="监控规则与LLM混合路由质量，处理多意图冲突、人工纠正反馈和死信重放；不确定退款意图始终转人工。"
    >
      <template #actions>
        <select v-model.number="days" class="days-select" @change="loadMetrics">
          <option :value="7">近7天</option><option :value="30">近30天</option><option :value="90">近90天</option>
        </select>
        <button class="btn secondary" @click="refreshAll"><AppIcon name="refresh" />刷新数据</button>
      </template>
    </PageHeader>

    <div class="metric-grid">
      <div class="metric-card">
        <div class="metric-top"><span>路由总量</span><span class="metric-icon"><AppIcon name="route" /></span></div>
        <div class="metric-value">{{ metrics.total ?? 0 }}</div>
        <div class="metric-foot">{{ days }}天观测窗口</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>规则命中率</span><span class="metric-icon green"><AppIcon name="check" /></span></div>
        <div class="metric-value">{{ pct(metrics.rule_hit_rate) }}</div>
        <div class="metric-foot">高置信请求避免LLM调用</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>LLM调用率</span><span class="metric-icon orange"><AppIcon name="activity" /></span></div>
        <div class="metric-value">{{ pct(metrics.llm_call_rate) }}</div>
        <div class="metric-foot">包含混合路由调用</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>冲突意图</span><span class="metric-icon red"><AppIcon name="warning" /></span></div>
        <div class="metric-value">{{ metrics.conflict_cases ?? 0 }}</div>
        <div class="metric-foot">需复核的多意图输入</div>
      </div>
    </div>

    <div v-if="error" class="notice danger"><AppIcon name="warning" />{{ error }}</div>

    <div class="segmented intent-tabs">
      <button :class="{ active: tab === 'lab' }" @click="tab = 'lab'">路由实验室</button>
      <button :class="{ active: tab === 'feedback' }" @click="tab = 'feedback'">纠正反馈</button>
      <button :class="{ active: tab === 'dead' }" @click="tab = 'dead'">死信队列</button>
    </div>

    <section v-if="tab === 'lab'" class="content-grid">
      <div class="card span-5">
        <div class="card-header"><div><div class="card-title">路由验证</div><div class="card-description">同时运行主路由与多意图冲突分析，结果不会修改业务状态。</div></div></div>
        <label>客户表达</label>
        <textarea v-model="routeText" rows="8" placeholder="例如：退款一直没到账，我还要投诉并转人工处理"></textarea>
        <label>关联案件ID（可选）</label>
        <input v-model="caseId" placeholder="用于生成关联证据" />
        <button class="btn run-route" :disabled="routeBusy || !routeText.trim()" @click="runRoute">
          <AppIcon name="route" />{{ routeBusy ? '分析中…' : '开始意图分析' }}
        </button>
      </div>
      <div class="card span-7 route-result">
        <div class="card-header"><div><div class="card-title">结构化路由证据</div><div class="card-description">置信度、来源、重试次数和冲突候选均来自后端路由器。</div></div></div>
        <div v-if="!routeResult" class="empty-state"><div><AppIcon name="route" /><strong>等待分析</strong><p>模糊退款表达将安全降级到HUMAN。</p></div></div>
        <template v-else>
          <div class="route-primary">
            <div><span>最终意图</span><strong>{{ routeResult.final_intent }}</strong></div>
            <div><span>置信度</span><strong>{{ pct(routeResult.confidence) }}</strong></div>
            <div><span>路由来源</span><strong>{{ routeResult.route_source || (routeResult.level_1_rule ? 'rule' : 'fallback') }}</strong></div>
            <div><span>风险级别</span><strong>{{ routeResult.risk }}</strong></div>
          </div>
          <div class="notice" :class="{ warning: multiResult?.conflict }">
            <AppIcon :name="multiResult?.conflict ? 'warning' : 'check'" />
            {{ multiResult?.conflict ? '检测到多意图冲突，需要人工复核或拆分处理。' : '未检测到需要升级的多意图冲突。' }}
          </div>
          <div class="candidate-list">
            <div v-for="candidate in multiResult?.candidates || []" :key="candidate.intent" class="candidate">
              <div><strong>{{ candidate.intent }}</strong><span>{{ candidate.hits }}个规则命中</span></div>
              <span class="tag" :class="candidate.confidence >= 0.8 ? 'green' : 'yellow'">{{ pct(candidate.confidence) }}</span>
            </div>
            <div v-if="!(multiResult?.candidates || []).length" class="muted">没有确定性规则候选，已由安全兜底处理。</div>
          </div>
          <div class="reason-box"><span>判定说明</span><p>{{ routeResult.reason || '—' }}</p></div>
        </template>
      </div>
    </section>

    <section v-else-if="tab === 'feedback'" class="card">
      <div class="card-header">
        <div><div class="card-title">人工纠正反馈</div><div class="card-description">审核通过的样本只进入评测候选集，不会自动修改生产规则。</div></div>
        <select v-model="feedbackStatus" class="compact-select" @change="loadFeedback">
          <option value="">全部状态</option><option value="PENDING">PENDING</option><option value="REVIEWED">REVIEWED</option><option value="ADOPTED">ADOPTED</option><option value="REJECTED">REJECTED</option>
        </select>
      </div>
      <div v-if="loadingFeedback" class="empty-state"><div class="spinner"></div></div>
      <div v-else-if="!feedback.length" class="empty-state"><div><AppIcon name="check" /><strong>暂无待审反馈</strong><p>人工纠正样本将在这里形成审核闭环。</p></div></div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>案件</th><th>原路由</th><th>人工纠正</th><th>原因</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="item in feedback" :key="item.id">
              <td class="mono">{{ shortId(item.case_id) }}</td>
              <td>{{ item.route_final_intent }}</td>
              <td><strong>{{ item.human_corrected_intent }}</strong></td>
              <td class="muted">{{ item.reason || '—' }}</td>
              <td><span class="tag" :class="feedbackClass(item.status)">{{ item.status }}</span></td>
              <td><div class="row-actions">
                <button v-if="item.status === 'PENDING'" class="btn small secondary" @click="reviewFeedback(item, 'REVIEWED')">进入复核</button>
                <template v-if="item.status === 'REVIEWED'">
                  <button class="btn small green" @click="reviewFeedback(item, 'ADOPTED')">采纳</button>
                  <button class="btn small red" @click="reviewFeedback(item, 'REJECTED')">驳回</button>
                </template>
              </div></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-else class="card">
      <div class="card-header">
        <div><div class="card-title">异常死信队列</div><div class="card-description">仅主管可人工重放；退款任务使用固定replay_guid保证外部副作用幂等。</div></div>
        <button class="btn secondary" @click="loadDeadLetters"><AppIcon name="refresh" />刷新队列</button>
      </div>
      <div class="notice warning"><AppIcon name="warning" />重放可能继续原任务。系统会先执行幂等检查，已关闭或正在执行的任务不会重复处理。</div>
      <div v-if="loadingDead" class="empty-state"><div class="spinner"></div></div>
      <div v-else-if="!deadLetters.length" class="empty-state"><div><AppIcon name="check" /><strong>当前没有死信任务</strong><p>异常恢复队列运行正常。</p></div></div>
      <div v-else class="table-wrap dead-table">
        <table>
          <thead><tr><th>任务ID</th><th>任务类型</th><th>状态</th><th>错误摘要</th><th>重试</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in deadLetters" :key="item.id">
            <td class="mono">{{ shortId(item.id) }}</td><td>{{ item.task_type }}</td>
            <td><span class="tag" :class="item.status === 'CLOSED' ? 'green' : 'red'">{{ item.status }}</span></td>
            <td class="dead-error" :title="item.error">{{ item.error || '—' }}</td>
            <td>{{ item.retry_count ?? 0 }}</td><td class="muted">{{ fmtTime(item.created_at) }}</td>
            <td><button class="btn small secondary" :disabled="item.status === 'CLOSED' || replaying === item.id" @click="replay(item)">{{ replaying === item.id ? '重放中…' : '安全重放' }}</button></td>
          </tr></tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import {
  intentAnalyzeMulti,
  intentGetMetrics,
  intentListDeadLetters,
  intentListFeedback,
  intentReplayDeadLetter,
  intentReviewFeedback,
  intentRoute,
} from '../api'

const tab = ref('lab')
const days = ref(7)
const metrics = ref({})
const error = ref('')
const routeText = ref('')
const caseId = ref('')
const routeResult = ref(null)
const multiResult = ref(null)
const routeBusy = ref(false)
const feedback = ref([])
const feedbackStatus = ref('')
const loadingFeedback = ref(false)
const deadLetters = ref([])
const loadingDead = ref(false)
const replaying = ref('')

function messageOf(e) {
  const detail = e.response?.data?.detail || e.response?.data?.message
  return typeof detail === 'string' ? detail : detail?.message || '请求失败，请稍后重试'
}

async function loadMetrics() {
  try {
    metrics.value = (await intentGetMetrics(days.value)).data || {}
  } catch (e) {
    error.value = messageOf(e)
  }
}

async function runRoute() {
  routeBusy.value = true
  error.value = ''
  try {
    const [route, multi] = await Promise.all([
      intentRoute(routeText.value, caseId.value || undefined),
      intentAnalyzeMulti(routeText.value),
    ])
    routeResult.value = route.data
    multiResult.value = multi.data
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    routeBusy.value = false
  }
}

async function loadFeedback() {
  loadingFeedback.value = true
  try {
    const { data } = await intentListFeedback(feedbackStatus.value || undefined)
    feedback.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loadingFeedback.value = false
  }
}

async function reviewFeedback(item, decision) {
  try {
    await intentReviewFeedback(item.id, decision, '意图可靠性中心人工审核')
    await loadFeedback()
    await loadMetrics()
  } catch (e) {
    error.value = messageOf(e)
  }
}

async function loadDeadLetters() {
  loadingDead.value = true
  try {
    const { data } = await intentListDeadLetters({ limit: 100 })
    deadLetters.value = data.items || []
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loadingDead.value = false
  }
}

async function replay(item) {
  if (!window.confirm('确认安全重放该死信任务？系统将执行幂等检查，审计记录会保留。')) return
  replaying.value = item.id
  try {
    await intentReplayDeadLetter(item.id)
    await loadDeadLetters()
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    replaying.value = ''
  }
}

async function refreshAll() {
  error.value = ''
  await Promise.all([loadMetrics(), loadFeedback(), loadDeadLetters()])
}

const pct = (value) => value == null ? '未测量' : (Number(value) * 100).toFixed(1) + '%'
const shortId = (value) => value ? String(value).slice(0, 8) : '—'
const fmtTime = (value) => value ? value.replace('T', ' ').slice(0, 19) : '—'
const feedbackClass = (value) => ({ PENDING: 'yellow', REVIEWED: 'blue', ADOPTED: 'green', REJECTED: 'red' }[value] || 'gray')

onMounted(refreshAll)
</script>

<style scoped>
.days-select, .compact-select { width: 120px; min-height: 36px; }
.intent-tabs { align-self: flex-start; }
.run-route { width: 100%; margin-top: 16px; }
.route-result { min-height: 430px; }
.route-primary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 9px; margin-bottom: 14px; }
.route-primary > div { padding: 12px; background: var(--panel2); border-radius: 10px; }
.route-primary span, .route-primary strong { display: block; }
.route-primary span { color: var(--muted); font-size: 11px; }
.route-primary strong { margin-top: 5px; color: var(--text-strong); font-size: 15px; overflow-wrap: anywhere; }
.candidate-list { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
.candidate { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px; }
.candidate div { display: flex; flex-direction: column; }
.candidate span:not(.tag) { color: var(--muted); font-size: 11px; }
.reason-box { margin-top: 16px; padding: 12px; background: #f7f9fc; border-radius: 10px; }
.reason-box span { color: var(--muted); font-size: 11px; }
.reason-box p { margin-top: 4px; color: #435169; }
.row-actions { display: flex; gap: 6px; }
.dead-table { margin-top: 14px; }
.dead-error { max-width: 290px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 800px) { .route-primary { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 480px) { .route-primary { grid-template-columns: 1fr; } }
</style>
