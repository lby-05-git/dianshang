<template>
  <!-- 加载失败 -->
  <div v-if="loadErr" class="card empty">
    <div class="empty-icon">⚠️</div>
    <div class="empty-text error-text">{{ loadErr }}</div>
    <button class="btn" style="margin-top: 12px" @click="retry">重试</button>
  </div>

  <!-- 加载中 -->
  <div v-else-if="!detail" class="card empty">
    <div class="spinner"></div>
    <div class="empty-hint">加载中…</div>
  </div>

  <div v-else>
    <!-- 顶部返回栏 -->
    <div class="detail-head">
      <a class="back-link" href="#/">← 返回工单列表</a>
      <div class="head-title">
        <span class="head-case">案件详情</span>
        <span class="tag" :class="statusTag">{{ statusText }}</span>
        <span class="tag" :class="decisionTag">{{ decisionText }}</span>
      </div>
      <span class="head-id mono">{{ detail.case_id }}</span>
    </div>

    <!-- 顶部：工单业务三态 -->
    <div class="card status-flow-card">
      <div class="status-flow-head">
        <div>
          <h3>📍 工单状态流转</h3>
          <p>展示业务流程状态；审批结论与退款到账状态仍独立记录</p>
        </div>
        <span class="tag" :class="statusTag">当前：{{ statusText }}</span>
      </div>

      <div class="case-status-flow" role="list" aria-label="工单状态流转：运行中、挂起中、已完成">
        <template v-for="(step, index) in statusFlowSteps" :key="step.key">
          <div
            class="case-status-step"
            :class="[step.state, `tone-${step.tone}`]"
            role="listitem"
            :aria-current="step.state === 'current' ? 'step' : undefined"
          >
            <span class="status-step-index" aria-hidden="true">
              {{ step.state === 'done' ? '✓' : step.state === 'skipped' ? '—' : index + 1 }}
            </span>
            <span class="status-step-copy">
              <strong>{{ step.label }}</strong>
              <small>{{ step.description }}</small>
            </span>
            <span class="status-step-badge">{{ step.badge }}</span>
          </div>
          <div
            v-if="index < statusFlowSteps.length - 1"
            class="status-flow-connector"
            :class="{ passed: statusConnectorPassed(index) }"
            aria-hidden="true"
          ><span></span></div>
        </template>
      </div>

      <div class="status-flow-summary">
        <span>流程状态：<strong>{{ statusText }}</strong></span>
        <span>业务决策：<strong>{{ decisionText }}</strong></span>
        <span>退款状态：<strong>{{ refundText }}</strong></span>
      </div>
    </div>

    <div class="grid">
      <!-- 左：Agent 流转图 -->
      <div class="card">
        <h3>🤖 Agent 状态流转图</h3>
        <FlowGraph :agents="detail.agents" :current-status="detail.current_status"
                   :current-node="detail.current_graph_node" :final-decision="detail.final_decision"
                   :refund-status="detail.refund_status" />
        <div class="legend">
          <span><i class="dot green"></i>完成</span>
          <span><i class="dot yellow"></i>挂起等待</span>
          <span><i class="dot gray"></i>未执行</span>
          <span><i class="dot refund"></i>退款：{{ refundText }}</span>
        </div>
      </div>

      <!-- 右：案件信息 + 风险 -->
      <div class="card">
        <h3>📋 案件信息</h3>
        <table class="kv">
          <tbody>
            <tr><td>订单号</td><td class="mono">{{ detail.order_id }}</td></tr>
            <tr><td>申请金额</td><td class="amount">¥{{ fmtMoney(detail.requested_amount) }}</td></tr>
            <tr><td>当前节点</td><td>{{ nodeText(detail.current_graph_node) || '-' }}</td></tr>
            <tr>
              <td>退款状态</td>
              <td><span class="tag" :class="refundTag">{{ refundText }}</span></td>
            </tr>
            <tr><td>创建时间</td><td>{{ fmt(detail.created_at) }}</td></tr>
          </tbody>
        </table>

        <!-- 核验结果（：订单/商品/价格） -->
        <h4>🛡️ 订单核验</h4>
        <table class="kv">
          <tbody>
            <tr v-for="v in verifications" :key="v.label">
              <td>{{ v.label }}</td>
              <td>
                <span class="tag" :class="verifyTag(v.value)">{{ verifyText(v.value) }}</span>
                <ul v-if="v.reasons && v.reasons.length" class="reasons">
                  <li v-for="(rs, i) in v.reasons" :key="i">{{ rs }}</li>
                </ul>
                <div v-else class="muted">暂无核验理由</div>
              </td>
            </tr>
          </tbody>
        </table>

        <h4>📊 三维决策评分</h4>
        <div class="score-grid">
          <article v-for="dimension in decisionDimensions" :key="dimension.key" class="score-card">
            <div class="score-head">
              <span>{{ dimension.label }}</span>
              <span class="score-value" :class="dimensionTag(dimension)">
                {{ dimension.score == null ? '评估中' : dimension.score }}<small v-if="dimension.score != null"> / 100</small>
              </span>
            </div>
            <div class="bar score-bar">
              <div class="bar-fill" :class="dimensionTag(dimension)" :style="{ width: (dimension.score ?? 0) + '%' }"></div>
            </div>
            <div class="score-foot">
              <span class="tag" :class="dimensionTag(dimension)">{{ dimensionLevelText(dimension) }}</span>
              <span>{{ dimension.explanation }}</span>
            </div>
          </article>
        </div>

        <h4>🛡️ 风险报告</h4>
        <table class="kv">
          <tbody>
            <tr>
              <td>欺诈分</td>
              <td>
                <template v-if="detail.fraud_score != null">
                  <span class="score-num" :class="fraudTag">{{ detail.fraud_score }}</span>
                  <span class="tag" :class="fraudTag">{{ fraudLevel }}</span>
                  <div class="bar"><div class="bar-fill" :class="fraudTag" :style="{ width: fraud_score + '%' }"></div></div>
                </template>
                <span v-else class="muted">-</span>
              </td>
            </tr>
            <tr>
              <td>舆情等级</td>
              <td>
                <span v-if="detail.sentiment_level" class="tag" :class="sentTag">{{ sentText }}</span>
                <span v-else class="muted">-</span>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- 退款回执（3.3.6） -->
        <h4>💸 退款回执</h4>
        <div v-if="refundTxs.length" class="receipt">
          <div v-for="tx in refundTxs" :key="tx.refund_id || tx.created_at" class="receipt-item">
            <span class="mono">{{ tx.refund_id }}</span>
            <span class="tag" :class="refundTagOf(tx.status)">{{ refundTextOf(tx.status) }}</span>
            <span class="amount">¥{{ fmtMoney(tx.amount) }}</span>
            <span v-if="tx.error" class="muted" :title="tx.error">有异常</span>
            <span class="muted">{{ fmt(tx.created_at) }}</span>
          </div>
        </div>
        <div v-else class="muted">尚未产生退款回执</div>

        <h4>🔍 订单凭证与 OCR 识别</h4>
        <div class="ocr-box">
          <section class="evidence-panel">
            <div class="ocr-section-head">
              <span>订单凭证原图</span>
              <span class="sensitive-tip">仅授权人员可见</span>
            </div>
            <button v-if="evidenceImageUrl" class="evidence-thumb" type="button" @click="evidencePreviewOpen = true">
              <img :src="evidenceImageUrl" alt="用户提交的订单凭证原图" />
              <span>点击查看大图</span>
            </button>
            <div v-else-if="evidenceLoading" class="evidence-empty">
              <div class="mini-spinner"></div>
              <span>正在读取凭证…</span>
            </div>
            <div v-else class="evidence-empty">
              <span>🖼️</span>
              <span>{{ evidenceError || '该案件未上传订单凭证' }}</span>
            </div>
          </section>

          <section class="ocr-result-panel">
            <div class="ocr-section-head"><span>OCR 识别结果</span></div>
            <template v-if="detail.ocr_summary && detail.ocr_summary.text">
              <div class="ocr-conf">
                <span>识别置信度</span>
                <span class="score-num" :class="confTag">{{ (detail.ocr_summary.confidence * 100).toFixed(1) }}%</span>
              </div>
              <div class="bar"><div class="bar-fill" :class="confTag" :style="{ width: (detail.ocr_summary.confidence * 100) + '%' }"></div></div>
              <div v-if="detail.ocr_summary.fields && Object.keys(detail.ocr_summary.fields).length" class="ocr-fields">
                <div><span>订单编号</span><strong>{{ detail.ocr_summary.fields.order_id || '未识别' }}</strong></div>
                <div><span>商品 SKU</span><strong>{{ detail.ocr_summary.fields.product_sku || '未识别' }}</strong></div>
                <div><span>商品名称</span><strong>{{ detail.ocr_summary.fields.product_name || '未识别' }}</strong></div>
                <div><span>订单金额</span><strong>{{ detail.ocr_summary.fields.amount ? `¥${detail.ocr_summary.fields.amount}` : '未识别' }}</strong></div>
              </div>
              <pre>{{ detail.ocr_summary.text.slice(0, 400) }}</pre>
            </template>
            <div v-else class="ocr-no-result">暂无 OCR 结果，可先核对左侧订单凭证原图</div>
          </section>
        </div>

        <!-- Agent 轨迹 -->
        <h4>🧩 Agent 执行轨迹</h4>
        <div v-if="sortedAgents.length" class="trail">
          <div v-for="a in sortedAgents" :key="a.agent_name + a.step_order" class="trail-item">
            <span class="trail-step">{{ a.step_order }}</span>
            <span class="trail-name">{{ a.agent_name }}</span>
            <span class="trail-status" :class="a.status === 'SUCCESS' ? 'green' : 'red'">
              {{ a.status === 'SUCCESS' ? '成功' : a.status }}
            </span>
            <span v-if="a.duration_ms != null" class="trail-ms">{{ a.duration_ms }}ms</span>
            <span v-if="traceSummary(a)" class="trail-summary" :title="traceDetail(a)">{{ traceSummary(a) }}</span>
          </div>
        </div>
        <div v-else class="muted">尚无轨迹</div>
      </div>

      <!-- 底部：人工审批面板（仅主管 + 挂起中） -->
      <div class="card approve-panel" :class="{ active: canApprove }">
        <h3>🖊️ 人工审批面板</h3>
        <template v-if="canApprove">
          <div class="approve-tip">⚠️ 该工单已挂起，等待主管裁决</div>
          <label>审批意见</label>
          <textarea v-model="opinion" rows="2" placeholder="例如：情况属实，批准退款"></textarea>
          <div class="approve-btns">
            <button class="btn green" :disabled="submitting" @click="doDecide('approve')">✓ 批准退款</button>
            <button class="btn red" :disabled="submitting" @click="doDecide('reject')">✗ 拒绝退款</button>
          </div>
          <div v-if="decideErr" class="error-text">{{ decideErr }}</div>
        </template>
        <template v-else>
          <div class="muted approve-hint">{{ approveHint }}</div>
        </template>
      </div>

      <div v-if="toast" class="toast">✅ {{ toast }}</div>
    </div>
  </div>

  <div v-if="evidencePreviewOpen && evidenceImageUrl" class="evidence-lightbox" role="dialog"
       aria-modal="true" aria-label="订单凭证原图预览" @click.self="evidencePreviewOpen = false">
    <button class="lightbox-close" type="button" aria-label="关闭预览" @click="evidencePreviewOpen = false">×</button>
    <img :src="evidenceImageUrl" alt="用户提交的订单凭证原图大图" />
    <div class="lightbox-caption">订单凭证原图 · 点击空白区域关闭</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { decide, getCase, getCaseEvidence, subscribeCaseEvents } from '../api'
import FlowGraph from '../components/FlowGraph.vue'

const route = useRoute()
const caseId = route.params.id
const detail = ref(null)
const loadErr = ref('')
const opinion = ref('')
const submitting = ref(false)
const decideErr = ref('')
const toast = ref('')
const role = localStorage.getItem('role')
const evidenceImageUrl = ref('')
const evidenceLoading = ref(false)
const evidenceError = ref('')
const evidencePreviewOpen = ref(false)

const canApprove = computed(
  () => role === 'supervisor' && detail.value?.current_status === 'SUSPENDED'
      && detail.value?.final_decision === 'PENDING'
)
const approveHint = computed(() => {
  if (role !== 'supervisor') return '当前账号无审批权限（仅主管可审批）'
  if (detail.value?.current_status !== 'SUSPENDED') return '案件未处于挂起状态，无需审批'
  return '案件已处理完毕'
})
const statusTag = computed(() => ({
  RUNNING: 'blue', SUSPENDED: 'yellow', COMPLETED: 'green',
}[detail.value.current_status] || 'gray'))
const statusText = computed(() => ({
  RUNNING: '运行中', SUSPENDED: '挂起待审', COMPLETED: '已完成',
}[detail.value.current_status] || detail.value.current_status))
const decisionTag = computed(() => ({
  PENDING: 'gray', APPROVED: 'green', REJECTED: 'red', SYSTEM_FAILURE: 'red',
}[detail.value.final_decision] || 'gray'))
const decisionText = computed(() => ({
  PENDING: '待决策', APPROVED: '已批准', REJECTED: '已拒绝', SYSTEM_FAILURE: '系统失败',
}[detail.value.final_decision] || detail.value.final_decision))
const fraud_score = computed(() => Math.min(100, Math.max(0, detail.value.fraud_score ?? 0)))
const fraudTag = computed(() => (detail.value.fraud_score >= 60 ? 'red' : detail.value.fraud_score <= 20 ? 'green' : 'yellow'))
const fraudLevel = computed(() => (detail.value.fraud_score >= 60 ? '高风险' : detail.value.fraud_score <= 20 ? '低风险' : '中风险'))
const sentTag = computed(() => ({ LOW: 'green', MEDIUM: 'yellow', HIGH: 'red' }[detail.value.sentiment_level] || 'gray'))
const sentText = computed(() => ({ LOW: '低', MEDIUM: '中', HIGH: '高' }[detail.value.sentiment_level] || detail.value.sentiment_level))
const confTag = computed(() => {
  const c = detail.value.ocr_summary?.confidence ?? 0
  return c >= 0.75 ? 'green' : c >= 0.6 ? 'yellow' : 'red'
})
// ---- 退款状态（3.3.6）----
const refundText = computed(() => refundTextOf(detail.value.refund_status))
const refundTag = computed(() => refundTagOf(detail.value.refund_status))
const refundTextOf = (s) => ({ NONE: '未发起', PENDING: '退款中', SUCCESS: '已退款', FAILED: '退款失败' }[s] || s || '-')
const refundTagOf = (s) => ({ NONE: 'gray', PENDING: 'yellow', SUCCESS: 'green', FAILED: 'red' }[s] || 'gray')

// ---- 工单业务三态：与 Agent 技术节点图分开，避免把流程状态和退款状态混为一谈 ----
const hadHumanReview = computed(() => {
  const agents = detail.value?.agents || []
  return agents.some((agent) => String(agent.agent_name || '').toLowerCase() === 'human_review')
    || detail.value?.current_graph_node === 'human_review'
})
const statusFlowSteps = computed(() => {
  const current = String(detail.value?.current_status || '').toUpperCase()
  const completed = current === 'COMPLETED'
  const suspended = current === 'SUSPENDED'
  return [
    {
      key: 'RUNNING',
      label: '运行中',
      tone: 'blue',
      state: current === 'RUNNING' ? 'current' : (suspended || completed ? 'done' : 'pending'),
      badge: current === 'RUNNING' ? '当前状态' : (suspended || completed ? '已经过' : '等待进入'),
      description: current === 'RUNNING'
        ? (nodeText(detail.value?.current_graph_node) || '多 Agent 正在自动处理')
        : '多 Agent 自动处理阶段',
    },
    {
      key: 'SUSPENDED',
      label: '挂起中',
      tone: 'yellow',
      state: suspended ? 'current' : (completed && hadHumanReview.value ? 'done' : (completed ? 'skipped' : 'pending')),
      badge: suspended ? '等待审批' : (completed && hadHumanReview.value ? '已复核' : (completed ? '未经过' : '按需进入')),
      description: suspended
        ? '等待主管完成批准或拒绝'
        : (completed && hadHumanReview.value ? '主管已完成人工复核' : '仅异常、高风险或证据不足时进入'),
    },
    {
      key: 'COMPLETED',
      label: '已完成',
      tone: 'green',
      state: completed ? 'current' : 'pending',
      badge: completed ? '当前状态' : '流程终态',
      description: completed ? `${decisionText.value} · ${refundText.value}` : '审批或自动处理结束后进入',
    },
  ]
})
function statusConnectorPassed(index) {
  const current = String(detail.value?.current_status || '').toUpperCase()
  if (index === 0) return current === 'SUSPENDED' || current === 'COMPLETED'
  return current === 'COMPLETED'
}
// ---- 核验结果----
const verifications = computed(() => [
  {
    label: '订单真实性', value: detail.value.order_verification,
    reasons: detail.value.order_verify_reasons,
  },
  {
    label: '商品一致性', value: detail.value.product_verification,
    reasons: detail.value.product_verify_reasons,
  },
  {
    label: '价格一致性', value: detail.value.price_verification,
    reasons: detail.value.price_verify_reasons,
  },
])
const verifyTag = (v) => ({ MATCH: 'green', MISMATCH: 'red', UNKNOWN: 'yellow' }[v] || 'gray')
const verifyText = (v) => ({ MATCH: '匹配', MISMATCH: '不匹配', UNKNOWN: '未知' }[v] || v || '-')
// ---- 三维评分：由后端使用已落库事实统一计算，前端只负责展示 ----
const decisionDimensions = computed(() => detail.value.decision_scores?.dimensions || [])
function dimensionTag(dimension) {
  const score = dimension.score
  if (score == null) return 'gray'
  if (dimension.direction === 'HIGHER_BETTER') {
    return score >= 75 ? 'green' : score >= 50 ? 'yellow' : 'red'
  }
  return score >= 60 ? 'red' : score <= 20 ? 'green' : 'yellow'
}
function dimensionLevelText(dimension) {
  if (dimension.score == null) return '等待计算'
  if (dimension.direction === 'HIGHER_BETTER') {
    return dimension.score >= 75 ? '高可信' : dimension.score >= 50 ? '中可信' : '低可信'
  }
  return { LOW: '低风险', MEDIUM: '中风险', HIGH: '高风险' }[dimension.level] || '已评分'
}
// ---- 退款回执（3.3.6）----
const refundTxs = computed(() => detail.value.refund_transactions || [])
const sortedAgents = computed(
  () => [...(detail.value.agents || [])].sort((a, b) => (a.step_order ?? 99) - (b.step_order ?? 99)),
)
// ---- Trace：每节点业务摘要----
function traceSummary(a) {
  const o = a.output
  if (!o || typeof o !== 'object') return ''
  const texts = []
  if (o.decision) texts.push(`决策:${o.decision}`)
  if (o.score != null) texts.push(`欺诈:${o.score}`)
  if (o.level) texts.push(`舆情:${o.level}`)
  if (o.refund_id) texts.push(`退款:${o.refund_id}`)
  if (o.amount != null && o.refund_id) texts.push(`¥${o.amount}`)
  if (o.verdict || o.order) texts.push('核验')
  if (o.error) texts.push(`err:${String(o.error).slice(0, 20)}`)
  return texts.join('  ') || ''
}
function traceDetail(a) {
  try { return JSON.stringify(a.output, null, 2) } catch (e) { return '' }
}
const nodeText = (n) => ({
  intake: 'Intake 受理', ocr: 'Evidence OCR识别', fraud: 'Fraud 欺诈风控',
  sentiment: 'Sentiment 舆情分析', amount_decision: '金额决策',
  human_review: 'HumanReview 人工审批', refund_execution: '退款执行',
}[n] || n)

const fmt = (t) => (t || '').replace('T', ' ').slice(0, 19)
const fmtMoney = (n) => Number(n).toFixed(2)

async function refresh() {
  try {
    const { data } = await getCase(caseId)
    detail.value = data
  } catch (e) {
    if (e.response?.status === 404) {
      toast.value = '案件不存在'
    } else {
      loadErr.value = e.response?.data?.message || '加载失败，请重试'
    }
  }
}

async function loadEvidence() {
  if (evidenceLoading.value || evidenceImageUrl.value) return
  evidenceLoading.value = true
  evidenceError.value = ''
  try {
    const { data } = await getCaseEvidence(caseId)
    evidenceImageUrl.value = URL.createObjectURL(data)
  } catch (e) {
    const backendMessage = e.response?.data?.message || e.response?.data?.detail || ''
    if (e.response?.status === 404 && /没有订单凭证|凭证文件不存在/.test(backendMessage)) {
      evidenceError.value = '该案件未上传订单凭证'
    } else if (e.response?.status === 404) {
      evidenceError.value = '后端尚未加载凭证查看接口，请重启全部服务'
    }
    else if (e.response?.status === 403) evidenceError.value = '当前账号无权查看订单凭证'
    else evidenceError.value = '订单凭证读取失败，请稍后重试'
  } finally {
    evidenceLoading.value = false
  }
}

function retry() {
  loadErr.value = ''
  refresh()
}

async function doDecide(action) {
  decideErr.value = ''
  if (!opinion.value.trim()) {
    decideErr.value = '请填写审批意见'
    return
  }
  submitting.value = true
  try {
    const idem = crypto.randomUUID // 幂等键：每次审批操作新生成（docs/06 ）
    const { data } = await decide(caseId, action, opinion.value.trim(), idem)
    toast.value = `审批已提交（${data.action}），等待系统完成退款…`
    setTimeout(() => (toast.value = ''), 5000)
    opinion.value = ''
    refresh()
  } catch (e) {
    decideErr.value = e.response?.data?.message || '审批失败'
  } finally {
    submitting.value = false
  }
}

let unsubscribe = null
onMounted(() => {
  refresh()
  loadEvidence()
  // SSE 事件驱动刷新（500ms 合并批），不轮询覆盖后端状态
  unsubscribe = subscribeCaseEvents(() => refresh())
})
onUnmounted(() => {
  if (unsubscribe) unsubscribe()
  if (evidenceImageUrl.value) URL.revokeObjectURL(evidenceImageUrl.value)
})
</script>

<style scoped>
/* 顶部返回栏 */
.detail-head {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 14px;
}
.back-link {
  color: var(--muted); font-size: 13px;
  padding: 6px 14px; border-radius: 999px;
  border: 1px solid var(--border);
  transition: all 0.15s;
}
.back-link:hover { color: var(--blue); border-color: rgba(37, 99, 235, 0.4); background: rgba(37, 99, 235, 0.06); }
.head-title { display: flex; align-items: center; gap: 8px; flex: 1; }
.head-case { font-size: 17px; font-weight: 700; }
.head-id { color: var(--muted); font-size: 12px; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
h3 { margin-bottom: 10px; }
h4 { margin-top: 16px; margin-bottom: 8px; color: var(--text); font-size: 13px; }
.kv td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
.kv td:first-child { color: var(--muted); width: 100px; letter-spacing: 0.3px; }
.mono { font-family: Consolas, monospace; }
.amount { font-weight: 700; font-size: 16px; color: var(--yellow); }

/* 风险/OCR 分数与进度条 */
.score-grid { display: grid; gap: 8px; }
.score-card {
  padding: 11px 12px; border: 1px solid var(--border); border-radius: 10px;
  background: linear-gradient(135deg, var(--panel2), rgba(37, 99, 235, 0.025));
}
.score-head, .score-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.score-head > span:first-child { font-weight: 650; font-size: 13px; }
.score-value { font-family: Consolas, monospace; font-size: 18px; font-weight: 800; }
.score-value small { color: var(--muted); font-size: 10px; font-weight: 500; }
.score-value.green { color: var(--green); }
.score-value.yellow { color: var(--yellow); }
.score-value.red { color: var(--red); }
.score-value.gray { color: var(--muted); font-size: 13px; }
.score-bar { margin: 9px 0; height: 7px; }
.score-foot { align-items: flex-start; color: var(--muted); font-size: 11px; line-height: 1.5; }
.score-foot > span:last-child { text-align: right; }
.score-num { font-family: Consolas, monospace; font-weight: 700; font-size: 15px; margin-right: 6px; }
.score-num.green { color: var(--green); }
.score-num.yellow { color: var(--yellow); }
.score-num.red { color: var(--red); }
.bar { height: 5px; border-radius: 3px; background: var(--panel2); margin-top: 6px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.4s; }
.bar-fill.green { background: var(--green); }
.bar-fill.yellow { background: var(--yellow); }
.bar-fill.red { background: var(--red); }

/* 核验理由 */
.reasons { margin: 6px 0 0 2px; padding-left: 16px; }
.reasons li { color: var(--muted); font-size: 12px; padding: 1px 0; }

/* 退款回执 */
.receipt { display: flex; flex-direction: column; gap: 6px; }
.receipt-item {
  display: flex; align-items: center; gap: 8px;
  background: var(--panel2); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 11px; font-size: 13px;
}
.receipt-item .amount { font-size: 13px; }

/* 订单凭证 + OCR */
.ocr-box {
  display: grid; grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr); gap: 12px;
  background: var(--panel2); border: 1px solid var(--border); border-radius: 10px; padding: 12px;
}
.evidence-panel, .ocr-result-panel {
  min-width: 0; border: 1px solid var(--border); border-radius: 9px; padding: 10px; background: var(--panel);
}
.ocr-section-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  margin-bottom: 9px; color: var(--text); font-size: 12px; font-weight: 700;
}
.sensitive-tip { color: var(--muted); font-size: 10px; font-weight: 500; }
.evidence-thumb {
  display: block; width: 100%; padding: 0; overflow: hidden; cursor: zoom-in;
  color: var(--muted); background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
}
.evidence-thumb img { display: block; width: 100%; height: 210px; object-fit: contain; background: #f8fafc; }
.evidence-thumb span { display: block; padding: 7px; font-size: 11px; border-top: 1px solid var(--border); }
.evidence-thumb:hover { border-color: rgba(37, 99, 235, 0.55); }
.evidence-empty {
  min-height: 210px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  color: var(--muted); background: var(--panel2); border: 1px dashed var(--border); border-radius: 8px; font-size: 12px;
}
.mini-spinner { width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
.ocr-conf { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
.ocr-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-top: 10px; }
.ocr-fields > div { padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
.ocr-fields span, .ocr-fields strong { display: block; }
.ocr-fields span { color: var(--muted); font-size: 10px; }
.ocr-fields strong { margin-top: 2px; overflow-wrap: anywhere; color: var(--text); font-size: 11px; }
.ocr-box pre { white-space: pre-wrap; margin-top: 8px; color: var(--text); font-size: 12px; }
.ocr-no-result { min-height: 210px; display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 12px; text-align: center; }
.evidence-lightbox {
  position: fixed; inset: 0; z-index: 1000; display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 40px; background: rgba(2, 6, 23, 0.88); backdrop-filter: blur(4px);
}
.evidence-lightbox img { max-width: min(1100px, 94vw); max-height: 82vh; object-fit: contain; border-radius: 10px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4); }
.lightbox-close {
  position: absolute; top: 18px; right: 24px; width: 40px; height: 40px; cursor: pointer;
  color: #fff; background: rgba(255, 255, 255, 0.12); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 50%; font-size: 27px;
}
.lightbox-caption { margin-top: 12px; color: rgba(255, 255, 255, 0.82); font-size: 12px; }

/* Agent 轨迹 */
.trail { display: flex; flex-direction: column; gap: 6px; }
.trail-item {
  display: flex; align-items: center; gap: 10px;
  background: var(--panel2); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 11px;
}
.trail-step {
  width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
  background: var(--blue); color: #fff; font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.trail-name { font-size: 13px; flex: 1; }
.trail-status { font-size: 12px; font-weight: 600; }
.trail-status.green { color: var(--green); }
.trail-status.red { color: var(--red); }
.trail-ms { color: var(--muted); font-size: 12px; font-family: Consolas, monospace; }
.trail-summary {
  color: var(--muted); font-size: 12px; text-align: right; margin-left: auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 45%;
  cursor: help; font-family: Consolas, monospace;
}

/* 审批面板 */
.approve-panel { grid-column: 1 / -1; }
.approve-panel.active {
  border-color: rgba(251, 191, 36, 0.5);
  box-shadow: 0 0 0 1px rgba(251, 191, 36, 0.4), 0 4px 20px rgba(251, 191, 36, 0.08);
}
.approve-tip { color: var(--yellow); font-size: 13px; margin-bottom: 8px; }
.approve-btns { display: flex; gap: 10px; margin-top: 12px; }
.approve-btns .btn { padding: 10px 22px; font-size: 14px; }
.approve-hint { padding: 6px 0; }

/* 工单三态流转：与流程、决策、退款三个状态口径保持一致 */
.status-flow-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  margin-bottom: 18px;
}
.status-flow-card { margin-bottom: 14px; }
.status-flow-head h3 { margin: 0 0 5px; }
.status-flow-head p { margin: 0; color: var(--muted); font-size: 12px; }
.case-status-flow {
  display: grid; grid-template-columns: minmax(0, 1fr) 64px minmax(0, 1fr) 64px minmax(0, 1fr);
  align-items: stretch; gap: 0; margin-bottom: 14px;
}
.case-status-step {
  position: relative; min-width: 0; display: flex; align-items: center; gap: 12px;
  padding: 16px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--panel2); transition: border-color 0.2s, box-shadow 0.2s, transform 0.2s;
}
.case-status-step.current {
  transform: translateY(-2px); background: var(--panel);
  box-shadow: 0 8px 22px rgba(15, 23, 42, 0.09);
}
.case-status-step.current.tone-blue { border-color: rgba(59, 130, 246, 0.65); box-shadow: 0 8px 22px rgba(59, 130, 246, 0.14); }
.case-status-step.current.tone-yellow { border-color: rgba(245, 158, 11, 0.7); box-shadow: 0 8px 22px rgba(245, 158, 11, 0.14); }
.case-status-step.current.tone-green { border-color: rgba(16, 185, 129, 0.65); box-shadow: 0 8px 22px rgba(16, 185, 129, 0.14); }
.case-status-step.done { border-color: rgba(16, 185, 129, 0.35); background: rgba(16, 185, 129, 0.055); }
.case-status-step.skipped { border-style: dashed; opacity: 0.72; }
.status-step-index {
  width: 34px; height: 34px; border-radius: 50%; flex: 0 0 34px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--muted); background: var(--panel); border: 1px solid var(--border);
  font: 700 14px/1 Consolas, monospace;
}
.case-status-step.current.tone-blue .status-step-index { color: #fff; background: var(--blue); border-color: var(--blue); }
.case-status-step.current.tone-yellow .status-step-index { color: #fff; background: var(--yellow); border-color: var(--yellow); }
.case-status-step.current.tone-green .status-step-index,
.case-status-step.done .status-step-index { color: #fff; background: var(--green); border-color: var(--green); }
.status-step-copy { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.status-step-copy strong { color: var(--text); font-size: 15px; }
.status-step-copy small { color: var(--muted); font-size: 11px; line-height: 1.45; }
.status-step-badge {
  margin-left: auto; align-self: flex-start; flex-shrink: 0; padding: 3px 7px;
  color: var(--muted); background: rgba(148, 163, 184, 0.11); border-radius: 999px; font-size: 10px;
}
.case-status-step.current.tone-blue .status-step-badge { color: var(--blue); background: rgba(59, 130, 246, 0.12); }
.case-status-step.current.tone-yellow .status-step-badge { color: var(--yellow); background: rgba(245, 158, 11, 0.12); }
.case-status-step.current.tone-green .status-step-badge,
.case-status-step.done .status-step-badge { color: var(--green); background: rgba(16, 185, 129, 0.1); }
.status-flow-connector { display: flex; align-items: center; padding: 0 9px; }
.status-flow-connector span { position: relative; width: 100%; height: 2px; background: var(--border); }
.status-flow-connector span::after {
  content: ''; position: absolute; top: 50%; right: -1px; width: 8px; height: 8px;
  border-top: 2px solid var(--border); border-right: 2px solid var(--border); transform: translateY(-50%) rotate(45deg);
}
.status-flow-connector.passed span { background: rgba(16, 185, 129, 0.65); }
.status-flow-connector.passed span::after { border-color: rgba(16, 185, 129, 0.75); }
.status-flow-summary {
  display: flex; flex-wrap: wrap; gap: 8px 22px; padding: 10px 13px;
  color: var(--muted); background: var(--panel2); border: 1px solid var(--border); border-radius: 9px; font-size: 12px;
}
.status-flow-summary strong { color: var(--text); }

/* 图例 */
.legend { display: flex; gap: 16px; margin-top: 8px; color: var(--muted); font-size: 12px; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; }
.dot.green { background: var(--green); }
.dot.yellow { background: var(--yellow); }
.dot.gray { background: var(--gray); }
.dot.refund { background: var(--yellow); }

/* 标签配色（与全局 .tag 兼容，补蓝色系） */
.tag.blue { background: rgba(96, 165, 250, 0.14); color: var(--blue); }
.tag.yellow { background: rgba(251, 191, 36, 0.14); color: var(--yellow); }
.tag.green { background: rgba(52, 211, 153, 0.14); color: var(--green); }
.tag.red { background: rgba(248, 113, 113, 0.14); color: var(--red); }
.tag.gray { background: rgba(148, 163, 184, 0.12); color: var(--muted); }

/* 空状态 / 加载态 */
.empty { text-align: center; padding: 48px 16px; }
.empty-icon { font-size: 40px; margin-bottom: 10px; opacity: 0.8; }
.empty-text { font-size: 15px; font-weight: 600; }
.empty-hint { color: var(--muted); font-size: 13px; margin-top: 6px; }
.spinner {
  width: 28px; height: 28px; margin: 8px auto 14px;
  border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.muted { color: var(--muted); }

@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
  .ocr-box { grid-template-columns: 1fr; }
  .evidence-lightbox { padding: 20px; }
  .case-status-flow { grid-template-columns: 1fr; gap: 8px; }
  .status-flow-connector { min-height: 24px; justify-content: center; padding: 0; }
  .status-flow-connector span { width: 2px; height: 24px; }
  .status-flow-connector span::after {
    top: auto; right: 50%; bottom: -1px; transform: translateX(50%) rotate(135deg);
  }
  .status-step-badge { margin-left: auto; }
}
</style>
