<template>
  <div class="page-stack">
    <PageHeader
      eyebrow="COMPLAINT REFUND OPERATIONS"
      title="工单决策总览"
      description="集中处理订单核验、OCR凭证、舆情风控、人工审批与退款执行，业务状态、决策状态和资金状态保持独立可追踪。"
    >
      <template #actions>
        <button class="btn secondary" :disabled="loading" @click="refresh"><AppIcon name="refresh" />刷新</button>
        <button class="btn" @click="showCreate = true"><AppIcon name="plus" />新建退款申请</button>
      </template>
    </PageHeader>

    <div class="metric-grid">
      <button v-for="st in statCards" :key="st.key" class="metric-card metric-button" :class="{ selected: curFilter === st.value }" @click="setFilter(st.value)">
        <div class="metric-top"><span>{{ st.label }}</span><span class="metric-icon" :class="st.color"><AppIcon :name="st.icon" /></span></div>
        <div class="metric-value">{{ stats[st.key] ?? 0 }}</div>
        <div class="metric-foot">{{ st.description }}</div>
      </button>
    </div>

    <section class="card cases-card">
      <div class="card-header">
        <div>
          <div class="card-title">退款工单</div>
          <div class="card-description">共 {{ total }} 条，按创建时间倒序排列</div>
        </div>
        <div class="segmented status-filter">
          <button v-for="item in filters" :key="item.value" :class="{ active: curFilter === item.value }" @click="setFilter(item.value)">{{ item.label }}</button>
        </div>
      </div>

      <div v-if="listErr" class="notice danger"><AppIcon name="warning" />{{ listErr }}</div>
      <div v-else-if="loading && !cases.length" class="empty-state"><div><div class="spinner"></div><p>正在同步工单数据…</p></div></div>
      <div v-else-if="!cases.length" class="empty-state">
        <div><AppIcon name="cases" /><strong>当前筛选下暂无工单</strong><p>新建退款申请后，系统会自动进入多Agent决策流程。</p><button class="btn empty-action" @click="showCreate = true"><AppIcon name="plus" />新建第一条工单</button></div>
      </div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>案件ID</th><th>订单号</th><th>申请金额</th><th>流程状态</th><th>业务决策</th><th>创建时间</th><th></th></tr></thead>
          <tbody>
            <tr v-for="item in cases" :key="item.case_id">
              <td><div class="case-id"><span class="case-avatar">{{ (item.order_id || 'O').slice(-2) }}</span><span><strong class="mono">{{ shortId(item.case_id) }}</strong><small>客诉退赔</small></span></div></td>
              <td class="mono">{{ item.order_id }}</td>
              <td class="money">{{ fmtMoney(item.requested_amount) }}</td>
              <td><span class="tag" :class="statusTag(item.current_status)">{{ statusText(item.current_status) }}</span></td>
              <td><span class="tag" :class="decisionTag(item.final_decision)">{{ decisionText(item.final_decision) }}</span></td>
              <td class="muted">{{ fmtTime(item.created_at) }}</td>
              <td><router-link class="detail-link" :to="'/cases/' + item.case_id">进入详情<AppIcon name="arrow" /></router-link></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="total > pageSize" class="pager">
        <button class="btn secondary" :disabled="page === 1" @click="goPage(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
        <button class="btn secondary" :disabled="page === totalPages" @click="goPage(page + 1)">下一页</button>
      </div>
    </section>

    <div v-if="showCreate" class="modal-mask" @click.self="closeCreate">
      <section class="card modal create-modal">
        <div class="modal-heading">
          <div><span class="modal-icon"><AppIcon name="plus" /></span><div><h3>新建退款申请</h3><p>提交后进入订单核验与多Agent决策链路</p></div></div>
          <button class="icon-btn" aria-label="关闭" @click="closeCreate"><AppIcon name="close" /></button>
        </div>
        <div class="notice"><AppIcon name="shield" />订单、商品或价格核验出现MISMATCH/UNKNOWN时，系统将挂起并转人工，不会自动退款。</div>
        <div class="field-row">
          <div><label>订单号 <em>*</em></label><input v-model.trim="form.order_id" placeholder="例如 ORD-20260901-001" /></div>
          <div><label>申请退款金额（元） <em>*</em></label><input v-model="form.amount" type="number" step="0.01" min="0.01" placeholder="0.00" /></div>
        </div>
        <label>退款原因</label>
        <textarea v-model.trim="form.reason" rows="3" placeholder="请描述客户诉求、商品问题和期望处理方式"></textarea>
        <label>凭证图片 <em>*</em></label>
        <label class="upload-zone" :class="{ filled: form.file }">
          <input ref="fileInput" type="file" accept=".jpg,.jpeg,.png" @change="onFile" />
          <template v-if="form.preview"><img :src="form.preview" alt="凭证预览" /><span><strong>{{ form.file.name }}</strong><small>点击可重新选择 · JPG/PNG，不超过5MB</small></span></template>
          <template v-else><span class="upload-icon"><AppIcon name="cases" /></span><span><strong>上传订单或聊天凭证</strong><small>点击选择JPG/PNG图片，最大5MB</small></span></template>
        </label>
        <div v-if="createErr" class="login-error"><AppIcon name="warning" />{{ createErr }}</div>
        <div class="modal-actions">
          <button class="btn secondary" :disabled="creating" @click="closeCreate">取消</button>
          <button class="btn" :disabled="creating" @click="doCreate">{{ creating ? '正在安全受理…' : '提交并进入决策流' }}<AppIcon name="arrow" /></button>
        </div>
      </section>
    </div>

    <div v-if="toast" class="toast"><strong>申请已受理</strong><div>{{ toast }}</div></div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import { createRefund, getStats, listCases, subscribeCaseEvents } from '../api'

const stats = reactive({ total: 0, running: 0, suspended: 0, completed: 0 })
const cases = ref([])
const total = ref(0)
const loading = ref(true)
const listErr = ref('')
const curFilter = ref('')
const showCreate = ref(false)
const creating = ref(false)
const createErr = ref('')
const toast = ref('')
const fileInput = ref(null)
const form = reactive({ order_id: '', amount: '', reason: '', file: null, preview: '' })
const page = ref(1)
const pageSize = 20
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

const filters = [
  { label: '全部工单', value: '' },
  { label: '运行中', value: 'RUNNING' },
  { label: '挂起待审', value: 'SUSPENDED' },
  { label: '已完成', value: 'COMPLETED' },
]
const statCards = [
  { key: 'total', value: '', label: '全部工单', icon: 'cases', color: '', description: '完整业务处理队列' },
  { key: 'running', value: 'RUNNING', label: '运行中', icon: 'activity', color: '', description: 'Agent正在分析与决策' },
  { key: 'suspended', value: 'SUSPENDED', label: '挂起待审', icon: 'warning', color: 'orange', description: '等待人工复核或补充材料' },
  { key: 'completed', value: 'COMPLETED', label: '已完成', icon: 'check', color: 'green', description: '业务流程已进入终态' },
]

let unsubscribe = null
let refreshing = false

function messageOf(e, fallback) {
  const detail = e.response?.data?.detail || e.response?.data?.message
  return typeof detail === 'string' ? detail : detail?.message || fallback
}

async function refresh() {
  if (refreshing) return
  refreshing = true
  loading.value = true
  try {
    const [statsRes, listRes] = await Promise.all([
      getStats(),
      listCases({ status: curFilter.value || undefined, page: page.value, size: pageSize }),
    ])
    Object.assign(stats, statsRes.data)
    cases.value = listRes.data.items || []
    total.value = listRes.data.total ?? cases.value.length
    listErr.value = ''
  } catch (e) {
    listErr.value = messageOf(e, '工单数据加载失败，请稍后重试')
  } finally {
    refreshing = false
    loading.value = false
  }
}

function setFilter(value) { curFilter.value = value; page.value = 1; refresh() }
function goPage(value) { if (value >= 1 && value <= totalPages.value) { page.value = value; refresh() } }

function onFile(event) {
  const file = event.target.files[0] || null
  if (!file) return
  if (!['image/jpeg', 'image/png'].includes(file.type)) {
    createErr.value = '仅支持JPG或PNG格式的凭证图片'
    event.target.value = ''
    return
  }
  if (file.size > 5 * 1024 * 1024) {
    createErr.value = '凭证图片不能超过5MB'
    event.target.value = ''
    return
  }
  if (form.preview) URL.revokeObjectURL(form.preview)
  form.file = file
  form.preview = URL.createObjectURL(file)
  createErr.value = ''
}

function resetForm() {
  if (form.preview) URL.revokeObjectURL(form.preview)
  Object.assign(form, { order_id: '', amount: '', reason: '', file: null, preview: '' })
  if (fileInput.value) fileInput.value.value = ''
  createErr.value = ''
}
function closeCreate() { if (!creating.value) { showCreate.value = false; resetForm() } }

async function doCreate() {
  createErr.value = ''
  if (!form.order_id || !form.amount || Number(form.amount) <= 0 || !form.file) {
    createErr.value = '请填写有效订单号、退款金额并上传凭证图片'
    return
  }
  creating.value = true
  try {
    const fd = new FormData()
    fd.append('order_id', form.order_id)
    fd.append('requested_amount', String(form.amount))
    if (form.reason) fd.append('refund_reason', form.reason)
    fd.append('evidence', form.file)
    const idem = crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random()
    const { data } = await createRefund(fd, idem)
    toast.value = '案件ID：' + data.case_id
    showCreate.value = false
    resetForm()
    await refresh()
    window.setTimeout(() => { toast.value = '' }, 4500)
  } catch (e) {
    createErr.value = messageOf(e, '创建失败，请核对订单与凭证后重试')
  } finally {
    creating.value = false
  }
}

const shortId = (value) => String(value || '').slice(0, 8)
const fmtTime = (value) => value ? value.replace('T', ' ').slice(0, 19) : '—'
const fmtMoney = (value) => '¥' + Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const statusTag = (value) => ({ RUNNING: 'blue', SUSPENDED: 'yellow', COMPLETED: 'green' }[value] || 'gray')
const statusText = (value) => ({ RUNNING: '运行中', SUSPENDED: '挂起待审', COMPLETED: '已完成' }[value] || value)
const decisionTag = (value) => ({ PENDING: 'gray', APPROVED: 'green', REJECTED: 'red', SYSTEM_FAILURE: 'red' }[value] || 'gray')
const decisionText = (value) => ({ PENDING: '待决策', APPROVED: '已批准', REJECTED: '已拒绝', SYSTEM_FAILURE: '系统失败' }[value] || value)

onMounted(() => { refresh(); unsubscribe = subscribeCaseEvents(refresh) })
onUnmounted(() => { unsubscribe?.(); if (form.preview) URL.revokeObjectURL(form.preview) })
</script>

<style scoped>
.metric-button { appearance: none; color: inherit; text-align: left; cursor: pointer; }
.metric-button:hover, .metric-button.selected { border-color: #aebff1; box-shadow: 0 7px 20px rgba(37,99,235,.08); }
.metric-button.selected { box-shadow: inset 0 0 0 1px #7797ef, 0 7px 20px rgba(37,99,235,.08); }
.cases-card { padding: 0; overflow: hidden; }
.cases-card .card-header { margin: 0; padding: 18px 19px; border-bottom: 1px solid var(--border); }
.cases-card .notice { margin: 16px; }
.cases-card .table-wrap { border: 0; border-radius: 0; }
.case-id { display: flex; align-items: center; gap: 9px; }
.case-avatar { width: 31px; height: 31px; display: grid; place-items: center; color: #315ac0; background: #eef3ff; border-radius: 8px; font-size: 10px; font-weight: 750; }
.case-id > span:last-child { display: flex; flex-direction: column; }
.case-id small { color: var(--muted-light); font-size: 9px; }
.money { color: var(--text-strong); font-weight: 680; }
.detail-link { display: inline-flex; align-items: center; gap: 5px; color: var(--blue); font-size: 12px; font-weight: 620; white-space: nowrap; }
.detail-link .app-icon { width: 14px; height: 14px; }
.pager { display: flex; align-items: center; justify-content: flex-end; gap: 12px; padding: 13px 18px; color: var(--muted); border-top: 1px solid var(--border); font-size: 12px; }
.empty-action { margin-top: 13px; }
.create-modal { width: min(620px, 100%); padding: 0; overflow: hidden; }
.modal-heading { display: flex; align-items: center; justify-content: space-between; padding: 18px 20px; border-bottom: 1px solid var(--border); }
.modal-heading > div:first-child { display: flex; align-items: center; gap: 11px; }
.modal-heading h3 { color: var(--text-strong); font-size: 17px; }
.modal-heading p { color: var(--muted); font-size: 11px; }
.modal-icon { width: 36px; height: 36px; display: grid; place-items: center; color: var(--blue); background: #edf3ff; border-radius: 10px; }
.create-modal > .notice, .create-modal > .field-row, .create-modal > label, .create-modal > textarea, .create-modal > .upload-zone, .create-modal > .login-error { margin-left: 20px; margin-right: 20px; }
.create-modal > .notice { margin-top: 16px; }
label em { color: var(--red); font-style: normal; }
.upload-zone { min-height: 92px; display: flex; align-items: center; gap: 13px; padding: 13px; color: var(--muted); background: #f8fafc; border: 1px dashed #bdc9da; border-radius: 11px; cursor: pointer; }
.upload-zone:hover { border-color: #7392e8; background: #f5f8ff; }
.upload-zone input { display: none; }
.upload-zone img { width: 70px; height: 64px; object-fit: cover; border-radius: 8px; }
.upload-zone > span:last-child { display: flex; flex-direction: column; }
.upload-zone strong { color: #435169; font-size: 12px; }
.upload-zone small { margin-top: 3px; color: var(--muted-light); font-size: 10px; }
.upload-icon { width: 42px; height: 42px; display: grid; place-items: center; color: var(--blue); background: #eaf1ff; border-radius: 10px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; padding: 14px 20px; background: #f8fafc; border-top: 1px solid var(--border); }
.toast strong { display: block; color: var(--green); font-size: 12px; }.toast div { margin-top: 2px; font-size: 11px; }
@media (max-width: 720px) {
  .status-filter { width: 100%; overflow: auto; }
  .cases-card .card-header { align-items: flex-start; flex-direction: column; }
}
</style>
