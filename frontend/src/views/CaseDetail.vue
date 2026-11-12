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

        <!-- Agent 轨迹：紧跟流转图，利用左侧纵向空间 -->
        <section class="agent-trail-section">
          <div class="agent-trail-head">
            <h4>🧩 Agent 执行轨迹</h4>
            <span v-if="sortedAgents.length" class="trail-count">{{ sortedAgents.length }} 个节点</span>
          </div>
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
          <div v-else class="muted trail-empty">尚无轨迹</div>
        </section>
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
            <ul v-if="dimension.reasons?.length" class="score-reasons">
              <li v-for="(reason, index) in dimension.reasons" :key="index">{{ reason }}</li>
            </ul>
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

        <h4>🧠 订单凭证多模态分析</h4>
        <div class="ocr-box">
          <section class="evidence-panel">
            <div class="ocr-section-head">
              <span>用户上传附件（{{ evidenceItems.length }}）</span>
              <span class="sensitive-tip">逐张分析 · 仅授权人员可见</span>
            </div>
            <button v-if="evidenceImageUrl" class="evidence-thumb" type="button" @click="evidencePreviewOpen = true">
              <img :src="evidenceImageUrl" :alt="selectedEvidence?.file_name || '用户上传附件'" />
              <span>{{ selectedEvidence?.file_name }} · 点击查看大图</span>
            </button>
            <div v-else-if="evidenceLoading" class="evidence-empty">
              <div class="mini-spinner"></div>
              <span>正在读取凭证…</span>
            </div>
            <div v-else class="evidence-empty">
              <span>🖼️</span>
              <span>{{ evidenceError || '该案件未上传订单凭证' }}</span>
            </div>
            <div v-if="evidenceItems.length > 1" class="evidence-gallery">
              <button v-for="item in evidenceItems" :key="item.evidence_id" type="button"
                      :class="{ active: selectedEvidence?.evidence_id === item.evidence_id }"
                      @click="selectedEvidence = item">
                <img :src="item.image_url" :alt="item.file_name" />
                <span>{{ item.file_name }}</span>
                <em :class="attachmentTag(item)">{{ attachmentVerdictText(item) }}</em>
              </button>
            </div>
          </section>

          <section class="ocr-result-panel">
            <div class="ocr-section-head">
              <div>
                <span>OCR 精确提取 + Qwen2.5-VL 视觉理解</span>
                <small v-if="selectedEvidence?.file_name">当前附件：{{ selectedEvidence.file_name }}</small>
              </div>
              <span class="tag" :class="evidenceStatusTag">{{ evidenceStatusText }}</span>
            </div>
            <template v-if="hasEvidenceAnalysis">
              <div v-if="selectedEvidenceAnalysis" class="attachment-analysis-strip">
                <span>当前附件分析</span>
                <strong>OCR：{{ selectedOcrStatusText }}</strong>
                <strong>Qwen2.5-VL：{{ selectedVisionStatusText }}</strong>
              </div>
              <div v-if="evidenceConsistency" class="consistency-card" :class="consistencyTone">
                <div class="consistency-head">
                  <div>
                    <span>退货申请与全部附件综合一致性</span>
                    <strong>{{ consistencyText }}</strong>
                  </div>
                  <span class="tag" :class="consistencyTag">{{ consistencyText }}</span>
                </div>
                <p>{{ evidenceConsistency.reason }}</p>
                <div class="consistency-dimensions">
                  <div v-for="(dimension, key) in evidenceConsistency.dimensions" :key="key">
                    <span>{{ consistencyDimensionLabel(key) }}</span>
                    <strong :class="dimensionVerdictTag(dimension.verdict)">{{ verifyText(dimension.verdict) }}</strong>
                    <small>{{ (dimension.reasons || []).join('；') }}</small>
                  </div>
                </div>
                <div v-if="evidenceConsistency.missing_evidence_types?.length" class="missing-evidence">
                  还需补充：{{ evidenceConsistency.missing_evidence_types.map(evidenceTypeLabel).join('、') }}
                </div>
              </div>
              <div class="model-score-grid">
                <div>
                  <span>综合可信度</span>
                  <strong>{{ formatConfidence(evidenceSummary.overall_confidence ?? evidenceSummary.confidence) }}</strong>
                </div>
                <div>
                  <span>OCR 置信度</span>
                  <strong>{{ formatConfidence(evidenceSummary.ocr?.confidence ?? evidenceSummary.confidence) }}</strong>
                </div>
                <div>
                  <span>视觉模型置信度</span>
                  <strong>{{ visionSucceeded ? formatConfidence(visionSummary.confidence) : '未获得结果' }}</strong>
                </div>
              </div>
              <div class="bar"><div class="bar-fill" :class="confTag" :style="{ width: confidenceWidth + '%' }"></div></div>

              <div v-if="evidenceSummary.fields && Object.keys(evidenceSummary.fields).length" class="ocr-fields">
                <div><span>订单编号</span><strong>{{ evidenceSummary.fields.order_id || '未识别' }}</strong></div>
                <div><span>运单编号</span><strong>{{ evidenceSummary.fields.tracking_no || evidenceSummary.fields.waybill_no || '未识别' }}</strong></div>
                <div><span>商品 SKU</span><strong>{{ evidenceSummary.fields.product_sku || '未识别' }}</strong></div>
                <div><span>商品名称</span><strong>{{ evidenceSummary.fields.product_name || '未识别' }}</strong></div>
                <div><span>订单金额</span><strong>{{ evidenceSummary.fields.amount ? `¥${evidenceSummary.fields.amount}` : '未识别' }}</strong></div>
                <div><span>字段来源</span><strong>{{ fieldSourceText }}</strong></div>
              </div>

              <div v-if="visionSucceeded" class="vision-analysis">
                <div class="vision-heading">
                  <span>视觉理解结果</span>
                  <span class="muted">{{ evidenceTypeText }}</span>
                </div>
                <div class="damage-summary">
                  <div>
                    <span>破损类型</span>
                    <strong>{{ damageCategoryText }}</strong>
                  </div>
                  <div>
                    <span>严重程度</span>
                    <strong>{{ damageSeverityText }}</strong>
                  </div>
                  <div>
                    <span>图片质量</span>
                    <strong>{{ imageQualityText }}</strong>
                  </div>
                </div>
                <ul v-if="evidenceSummary.visual_facts?.length" class="visual-facts">
                  <li v-for="(fact, index) in evidenceSummary.visual_facts" :key="index">{{ fact }}</li>
                </ul>
                <div class="alignment-row">
                  <span>与用户描述一致性</span>
                  <span class="tag" :class="alignmentTag">{{ alignmentText }}</span>
                </div>
                <p class="alignment-reason">{{ evidenceSummary.claim_alignment?.reason || '视觉模型未提供判断依据' }}</p>
              </div>

              <div v-if="evidenceSummary.prompt_injection_detected" class="evidence-alert danger">
                已检测到图片 Prompt 注入内容：该图片不会直接驱动退款，案件已进入安全兜底。
              </div>
              <div v-if="evidenceSummary.field_conflicts?.length" class="evidence-alert warning">
                <strong>发现 OCR 与视觉字段冲突：</strong>
                <span v-for="item in evidenceSummary.field_conflicts" :key="item.field">
                  {{ fieldLabel(item.field) }}（OCR：{{ item.ocr_value }}；视觉：{{ item.vision_value }}）
                </span>
              </div>
              <div v-if="!visionSucceeded && visionSummary.status" class="evidence-alert warning">
                视觉模型未完成（{{ visionStatusText }}），系统已按 OCR 结果降级处理；涉及不确定证据时转人工。
              </div>

              <details v-if="evidenceSummary.text" class="ocr-raw">
                <summary>查看 OCR 原始文本</summary>
                <pre>{{ evidenceSummary.text.slice(0, 600) }}</pre>
              </details>
            </template>
            <div v-else class="ocr-no-result">暂无多模态分析结果，可先核对左侧订单凭证原图</div>
          </section>
        </div>

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
    <img :src="evidenceImageUrl" :alt="selectedEvidence?.file_name || '用户上传附件大图'" />
    <div class="lightbox-caption">{{ selectedEvidence?.file_name }} · 点击空白区域关闭</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { decide, getCase, getCaseEvidence, getCaseEvidenceById, getCaseEvidences, subscribeCaseEvents } from '../api'
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
const evidenceItems = ref([])
const selectedEvidence = ref(null)
const evidenceImageUrl = computed(() => selectedEvidence.value?.image_url || '')
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
const aggregateEvidenceSummary = computed(() => detail.value?.ocr_summary || {})
const selectedEvidenceAnalysis = computed(() => selectedEvidence.value?.analysis || null)
const evidenceSummary = computed(() => {
  const aggregate = aggregateEvidenceSummary.value
  if (aggregate && Object.keys(aggregate).length) return aggregate
  return selectedEvidenceAnalysis.value || {}
})
const evidenceConsistency = computed(() => aggregateEvidenceSummary.value.consistency || null)
const selectedOcrStatusText = computed(() => ({
  SUCCESS: '提取完成', PENDING: '分析中', FAILED: '提取失败',
}[selectedEvidenceAnalysis.value?.ocr?.status || selectedEvidence.value?.ocr_status] || '等待结果'))
const selectedVisionStatusText = computed(() => {
  const analysis = selectedEvidenceAnalysis.value || {}
  const status = analysis.vision?.status
  if (analysis.structured_voucher && analysis.ocr?.status === 'SUCCESS'
      && ['INVALID_OUTPUT', 'TIMEOUT', 'FAILED', 'CIRCUIT_OPEN'].includes(status)) {
    return 'OCR 已完成凭证核验（视觉结果已降级）'
  }
  return ({
    SUCCESS: '理解完成', PENDING: '分析中', TIMEOUT: '推理超时',
    CIRCUIT_OPEN: '服务熔断', DISABLED: '未启用', INVALID_OUTPUT: '输出格式异常',
    FAILED: '分析失败',
  }[status] || '等待结果')
})
const consistencyText = computed(() => ({
  CONSISTENT: '一致', CONFLICT: '存在冲突', INSUFFICIENT: '证据不足',
  UNRELATED: '附件无关', UNSAFE: '附件不安全',
}[evidenceConsistency.value?.verdict] || '等待综合判断'))
const consistencyTag = computed(() => ({
  CONSISTENT: 'green', CONFLICT: 'red', INSUFFICIENT: 'yellow',
  UNRELATED: 'red', UNSAFE: 'red',
}[evidenceConsistency.value?.verdict] || 'gray'))
const consistencyTone = computed(() => `tone-${consistencyTag.value}`)
const consistencyDimensionLabel = (key) => ({
  order: '订单一致性', product: '商品一致性', price: '金额一致性', reason: '退货原因一致性',
  cross_attachment: '多附件交叉一致性',
}[key] || key)
const dimensionVerdictTag = (verdict) => ({
  MATCH: 'good', MISMATCH: 'bad', UNKNOWN: 'unknown',
}[verdict] || 'unknown')
const evidenceTypeLabel = (type) => ({
  order_receipt: '订单凭证', logistics_waybill: '物流运单', product_photo: '商品实拍',
  damage_photo: '破损实拍', chat_screenshot: '聊天截图', other: '其他凭证',
}[type] || type)
function attachmentVerdictText(item) {
  if (item.analysis?.structured_voucher && item.analysis?.ocr?.status === 'SUCCESS') {
    return '订单信息已核验'
  }
  const verdict = item.analysis?.claim_alignment?.verdict
  return ({ SUPPORTED: '支持申请', CONTRADICTED: '存在矛盾', INSUFFICIENT: '证据不足',
    NOT_APPLICABLE: '辅助凭证' }[verdict] || (item.ocr_status === 'PENDING' ? '分析中' : '待判断'))
}
function attachmentTag(item) {
  if (item.analysis?.structured_voucher && item.analysis?.ocr?.status === 'SUCCESS') return 'good'
  const verdict = item.analysis?.claim_alignment?.verdict
  return ({ SUPPORTED: 'good', CONTRADICTED: 'bad', INSUFFICIENT: 'unknown',
    NOT_APPLICABLE: 'neutral' }[verdict] || 'neutral')
}
const visionSummary = computed(() => evidenceSummary.value.vision || {})
const visionSucceeded = computed(() => visionSummary.value.status === 'SUCCESS')
const hasEvidenceAnalysis = computed(() => Boolean(
  evidenceSummary.value.version
  || evidenceSummary.value.text
  || evidenceSummary.value.vision?.status,
))
const evidenceStatusCode = computed(() => (
  evidenceSummary.value.evidence_status
  || (evidenceSummary.value.text ? 'SUCCESS' : 'PENDING')
))
const evidenceStatusText = computed(() => ({
  SUCCESS: '分析完成', DEGRADED: '降级可用', UNRELIABLE: '结果不可靠',
  FAILED: '分析失败', UNSAFE: '安全拦截', PENDING: '等待分析',
}[evidenceStatusCode.value] || evidenceStatusCode.value))
const evidenceStatusTag = computed(() => ({
  SUCCESS: 'green', DEGRADED: 'yellow', UNRELIABLE: 'red',
  FAILED: 'red', UNSAFE: 'red', PENDING: 'gray',
}[evidenceStatusCode.value] || 'gray'))
const confidenceValue = computed(() => {
  const raw = evidenceSummary.value.overall_confidence ?? evidenceSummary.value.confidence ?? 0
  return Math.min(1, Math.max(0, Number(raw) || 0))
})
const confidenceWidth = computed(() => (confidenceValue.value * 100).toFixed(1))
const confTag = computed(() => {
  const c = confidenceValue.value
  return c >= 0.75 ? 'green' : c >= 0.6 ? 'yellow' : 'red'
})
function formatConfidence(value) {
  if (value == null || Number.isNaN(Number(value))) return '-'
  return `${(Math.min(1, Math.max(0, Number(value))) * 100).toFixed(1)}%`
}
const damageCategoryText = computed(() => ({
  NONE: '未发现明确破损', PACKAGING_TRANSPORT_DAMAGE: '包装或运输破损',
  ACCESSORY_DAMAGE: '配件损坏', LIQUID_MOISTURE: '进液或受潮',
  DISPLAY_FAILURE: '屏幕显示故障', CAMERA_LENS_DAMAGE: '摄像头或镜头损坏',
  BODY_BACK_FRAME_DAMAGE: '后盖、边框或机身损坏', SCREEN_CRACK: '屏幕碎裂',
  SCRATCH_WEAR: '划痕或磨损', OTHER: '其他破损', UNKNOWN: '无法确定',
}[evidenceSummary.value.damage?.category] || evidenceSummary.value.damage?.type || '无法确定'))
const damageSeverityText = computed(() => ({
  none: '未发现', light: '轻微', medium: '中等', severe: '严重', unknown: '无法确定',
}[evidenceSummary.value.damage?.severity] || '无法确定'))
const evidenceTypeText = computed(() => ({
  order_receipt: '订单凭证', logistics_waybill: '物流运单', product_photo: '商品实拍',
  damage_photo: '破损实拍', chat_screenshot: '聊天截图', other: '其他凭证',
}[visionSummary.value.evidence_type] || '凭证类型待确认'))
const imageQualityText = computed(() => {
  const quality = visionSummary.value.image_quality
  if (!quality) return '无法确定'
  if (quality.occluded) return '存在遮挡'
  return quality.clear ? '清晰可辨' : '较模糊'
})
const alignmentText = computed(() => ({
  SUPPORTED: '支持用户描述', CONTRADICTED: '与描述矛盾',
  INSUFFICIENT: '证据不足', NOT_APPLICABLE: '不适用',
}[evidenceSummary.value.claim_alignment?.verdict] || '尚未判断'))
const alignmentTag = computed(() => ({
  SUPPORTED: 'green', CONTRADICTED: 'red', INSUFFICIENT: 'yellow', NOT_APPLICABLE: 'gray',
}[evidenceSummary.value.claim_alignment?.verdict] || 'gray'))
const visionStatusText = computed(() => ({
  TIMEOUT: '推理超时', CIRCUIT_OPEN: '服务熔断', DISABLED: '功能未启用',
  INVALID_OUTPUT: '模型输出格式异常', FAILED: '服务失败',
}[visionSummary.value.status] || visionSummary.value.status || '未知异常'))
const fieldSourceText = computed(() => {
  const sources = Object.values(evidenceSummary.value.field_sources || {})
  if (!sources.length) return '未记录'
  const unique = [...new Set(sources)]
  return unique.map((source) => ({ ocr: 'OCR', vision: '视觉模型', 'ocr+vision': 'OCR + 视觉模型' }[source] || source)).join('、')
})
const fieldLabel = (field) => ({
  order_id: '订单编号', tracking_no: '运单编号', waybill_no: '运单编号',
  product_sku: '商品 SKU', product_name: '商品名称', amount: '订单金额',
}[field] || field)
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
  intake: 'Intake 受理', ocr: 'Evidence 多模态凭证', fraud: 'Fraud 欺诈风控',
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

async function loadEvidence(force = false) {
  if (evidenceLoading.value || (!force && evidenceItems.value.length)) return
  evidenceLoading.value = true
  evidenceError.value = ''
  try {
    const { data } = await getCaseEvidences(caseId)
    const rows = data.items || []
    const previousItems = evidenceItems.value
    const previousById = new Map(previousItems.map((item) => [item.evidence_id, item]))
    const selectedId = selectedEvidence.value?.evidence_id
    const nextItems = await Promise.all(rows.map(async (item) => {
      const previous = previousById.get(item.evidence_id)
      if (previous?.image_url) return { ...item, image_url: previous.image_url }
      const response = await getCaseEvidenceById(caseId, item.evidence_id)
      return { ...item, image_url: URL.createObjectURL(response.data) }
    }))
    const nextIds = new Set(nextItems.map((item) => item.evidence_id))
    previousItems.forEach((item) => {
      if (!nextIds.has(item.evidence_id) && item.image_url) URL.revokeObjectURL(item.image_url)
    })
    evidenceItems.value = nextItems
    const preferredItem = nextItems.find((item) => item.analysis?.vision?.status === 'SUCCESS') || nextItems[0]
    selectedEvidence.value = nextItems.find((item) => item.evidence_id === selectedId) || preferredItem || null
    if (!nextItems.length) evidenceError.value = '该案件未上传任何附件'
  } catch (e) {
    // 兼容仍在运行的旧后端镜像：旧版只有单附件读取接口。这里至少展示
    // 自动生成的订单凭证；后端重建后会自动切回上面的多附件列表接口。
    if (e.response?.status === 404) {
      try {
        const response = await getCaseEvidence(caseId)
        const fallback = {
          evidence_id: 'legacy-primary',
          file_name: '订单凭证原图',
          image_url: URL.createObjectURL(response.data),
          legacy: true,
        }
        evidenceItems.value = [fallback]
        selectedEvidence.value = fallback
        evidenceError.value = ''
        return
      } catch (fallbackError) {
        e = fallbackError
      }
    }
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
  unsubscribe = subscribeCaseEvents(() => {
    refresh()
    // Worker 完成 OCR/VL 后分析结果写在 Evidence 上；同步刷新逐张元数据，
    // 但复用已经创建的图片 URL，避免重复下载原图和产生 Blob 泄漏。
    loadEvidence(true)
  })
})
onUnmounted(() => {
  if (unsubscribe) unsubscribe()
  evidenceItems.value.forEach((item) => item.image_url && URL.revokeObjectURL(item.image_url))
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
.score-reasons {
  margin: 9px 0 0; padding: 8px 10px 8px 26px; border-radius: 8px;
  background: color-mix(in srgb, var(--panel2) 78%, transparent);
  color: var(--muted); font-size: 11px; line-height: 1.65;
}
.score-reasons li::marker { color: var(--blue); }
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

/* 订单凭证 + OCR/Qwen-VL 多模态分析 */
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
.ocr-section-head > div span, .ocr-section-head > div small { display: block; }
.ocr-section-head > div small {
  max-width: 360px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--muted); font-size: 9px; font-weight: 500;
}
.attachment-analysis-strip {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  margin-bottom: 9px; padding: 7px 9px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--panel2); font-size: 10px;
}
.attachment-analysis-strip > span { color: var(--muted); }
.attachment-analysis-strip > strong { color: var(--text); font-weight: 650; }
.sensitive-tip { color: var(--muted); font-size: 10px; font-weight: 500; }
.evidence-thumb {
  display: block; width: 100%; padding: 0; overflow: hidden; cursor: zoom-in;
  color: var(--muted); background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
}
.evidence-thumb img { display: block; width: 100%; height: 210px; object-fit: contain; background: #f8fafc; }
.evidence-thumb span { display: block; padding: 7px; font-size: 11px; border-top: 1px solid var(--border); }
.evidence-thumb:hover { border-color: rgba(37, 99, 235, 0.55); }
.evidence-gallery {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-top: 9px;
}
.evidence-gallery button {
  min-width: 0; padding: 5px; cursor: pointer; text-align: left; color: var(--text);
  background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
}
.evidence-gallery button.active { border-color: var(--blue); box-shadow: 0 0 0 1px rgba(37, 99, 235, 0.18); }
.evidence-gallery img { width: 100%; height: 58px; object-fit: cover; border-radius: 5px; background: #f8fafc; }
.evidence-gallery span, .evidence-gallery em { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.evidence-gallery span { margin-top: 4px; font-size: 10px; }
.evidence-gallery em { margin-top: 2px; font-size: 9px; font-style: normal; }
.evidence-gallery em.good { color: var(--green); }
.evidence-gallery em.bad { color: var(--red); }
.evidence-gallery em.unknown { color: var(--yellow); }
.evidence-gallery em.neutral { color: var(--muted); }
.evidence-empty {
  min-height: 210px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  color: var(--muted); background: var(--panel2); border: 1px dashed var(--border); border-radius: 8px; font-size: 12px;
}
.mini-spinner { width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
.ocr-conf { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
.model-score-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-bottom: 8px;
}
.consistency-card {
  margin-bottom: 10px; padding: 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel2);
}
.consistency-card.tone-green { border-color: rgba(16, 185, 129, 0.38); background: rgba(16, 185, 129, 0.055); }
.consistency-card.tone-yellow { border-color: rgba(245, 158, 11, 0.4); background: rgba(245, 158, 11, 0.06); }
.consistency-card.tone-red { border-color: rgba(239, 68, 68, 0.38); background: rgba(239, 68, 68, 0.055); }
.consistency-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.consistency-head div > span, .consistency-head div > strong { display: block; }
.consistency-head div > span { color: var(--muted); font-size: 10px; }
.consistency-head div > strong { margin-top: 2px; font-size: 15px; }
.consistency-card > p { margin: 7px 0 9px; color: var(--muted); font-size: 11px; line-height: 1.45; }
.consistency-dimensions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.consistency-dimensions > div { padding: 7px 8px; border-radius: 7px; background: var(--panel); }
.consistency-dimensions span, .consistency-dimensions strong, .consistency-dimensions small { display: block; }
.consistency-dimensions span { color: var(--muted); font-size: 9px; }
.consistency-dimensions strong { margin-top: 2px; font-size: 11px; }
.consistency-dimensions strong.good { color: var(--green); }
.consistency-dimensions strong.bad { color: var(--red); }
.consistency-dimensions strong.unknown { color: var(--yellow); }
.consistency-dimensions small { margin-top: 2px; color: var(--muted); font-size: 9px; line-height: 1.35; }
.missing-evidence { margin-top: 8px; padding: 7px 8px; color: #92400e; background: rgba(245, 158, 11, 0.1); border-radius: 6px; font-size: 10px; }
.model-score-grid > div {
  min-width: 0; padding: 8px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel2);
}
.model-score-grid span, .model-score-grid strong { display: block; }
.model-score-grid span { color: var(--muted); font-size: 10px; }
.model-score-grid strong { margin-top: 3px; color: var(--text); font-size: 13px; overflow-wrap: anywhere; }
.ocr-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-top: 10px; }
.ocr-fields > div { padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
.ocr-fields span, .ocr-fields strong { display: block; }
.ocr-fields span { color: var(--muted); font-size: 10px; }
.ocr-fields strong { margin-top: 2px; overflow-wrap: anywhere; color: var(--text); font-size: 11px; }
.vision-analysis {
  margin-top: 10px; padding: 10px; border: 1px solid rgba(37, 99, 235, 0.24);
  border-radius: 9px; background: rgba(37, 99, 235, 0.04);
}
.vision-heading, .alignment-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.vision-heading { margin-bottom: 8px; color: var(--text); font-size: 12px; font-weight: 700; }
.damage-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.damage-summary > div { padding: 7px 8px; border-radius: 7px; background: var(--panel); }
.damage-summary span, .damage-summary strong { display: block; }
.damage-summary span { color: var(--muted); font-size: 10px; }
.damage-summary strong { margin-top: 2px; color: var(--text); font-size: 11px; overflow-wrap: anywhere; }
.visual-facts { margin: 9px 0 7px; padding-left: 18px; color: var(--text); font-size: 12px; }
.visual-facts li { margin: 3px 0; line-height: 1.45; }
.alignment-row { margin-top: 8px; color: var(--muted); font-size: 11px; }
.alignment-reason { margin: 6px 0 0; color: var(--text); font-size: 11px; line-height: 1.5; }
.evidence-alert {
  display: flex; flex-wrap: wrap; gap: 4px 8px; margin-top: 9px; padding: 9px 10px;
  border: 1px solid; border-radius: 8px; font-size: 11px; line-height: 1.45;
}
.evidence-alert.warning { color: #92400e; border-color: rgba(245, 158, 11, 0.35); background: rgba(245, 158, 11, 0.09); }
.evidence-alert.danger { color: #b91c1c; border-color: rgba(239, 68, 68, 0.35); background: rgba(239, 68, 68, 0.09); }
.ocr-raw { margin-top: 9px; border-top: 1px solid var(--border); padding-top: 8px; }
.ocr-raw summary { cursor: pointer; color: var(--muted); font-size: 11px; }
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
.agent-trail-section {
  margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border);
}
.agent-trail-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 10px;
}
.agent-trail-head h4 { margin: 0; }
.trail-count {
  padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px;
  color: var(--muted); background: var(--panel2); font-size: 12px;
}
.agent-trail-section .trail { max-height: 420px; overflow-y: auto; padding-right: 4px; }
.trail-empty { padding: 18px 0; text-align: center; }
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
  .model-score-grid, .damage-summary { grid-template-columns: 1fr; }
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
