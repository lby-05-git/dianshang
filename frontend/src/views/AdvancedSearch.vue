<template>
  <div class="search">
    <!-- 高级检索条件区 -->
    <div class="card filter-card">
      <div class="head">
        <h3>🔍 工单高级检索</h3>
        <div class="head-actions">
          <button class="btn gray" @click="reset">重置</button>
          <button class="btn green" :disabled="!canExport" title="需 leader+ 权限" @click="doExport">⬇ 导出CSV</button>
          <button class="btn" @click="openSave">💾 保存筛选器</button>
        </div>
      </div>

      <div class="grid">
        <label>工单号 / 关键词
          <input v-model="f.keyword" class="full" placeholder="单号 / 诉求关键词" />
        </label>
        <label>状态
          <select v-model="f.status">
            <option value="">全部</option>
            <option value="RUNNING">运行中</option>
            <option value="SUSPENDED">挂起待审</option>
            <option value="COMPLETED">已完成</option>
          </select>
        </label>
        <label>决策
          <select v-model="f.decision">
            <option value="">全部</option>
            <option value="PENDING">待决策</option>
            <option value="APPROVED">已批准</option>
            <option value="REJECTED">已拒绝</option>
            <option value="SYSTEM_FAILURE">系统失败</option>
          </select>
        </label>
        <label>渠道
          <select v-model="f.channel">
            <option value="">全部</option>
            <option value="APP">APP</option>
            <option value="小程序">小程序</option>
            <option value="客服代提">客服代提</option>
          </select>
        </label>
        <label>金额下限（¥）
          <input v-model="f.amount_min" type="number" min="0" />
        </label>
        <label>金额上限（¥）
          <input v-model="f.amount_max" type="number" min="0" />
        </label>
        <label>原因码
          <input v-model="f.reason_code" placeholder="如 WAREHOUSE / LATE" />
        </label>
        <label>排序字段
          <select v-model="f.sort_by">
            <option value="created_at">创建时间</option>
            <option value="requested_amount">金额</option>
            <option value="sla_deadline_at">SLA截止</option>
            <option value="updated_at">更新时间</option>
          </select>
        </label>
        <label>排序方向
          <select v-model="f.sort_order">
            <option value="desc">倒序</option>
            <option value="asc">正序</option>
          </select>
        </label>
      </div>

      <div class="actions">
        <button class="btn" :disabled="loading" @click="doSearch">
          {{ loading ? '检索中…' : '🔎 检索' }}
        </button>
      </div>
    </div>

    <!-- 保存的筛选器 -->
    <div class="card saved-card" v-if="savedFilters.length">
      <div class="head"><h3>📑 已保存筛选器</h3></div>
      <div class="chips">
        <span v-for="s in savedFilters" :key="s.id" class="chip">
          <button class="chip-btn" @click="applySaved(s)">{{ s.name }}</button>
          <button class="chip-del" title="删除" @click="delSaved(s.id)">×</button>
        </span>
      </div>
    </div>

    <!-- 结果列表 -->
    <div class="card list-card">
      <div class="head">
        <h3>📋 检索结果</h3>
        <span class="count" v-if="total">{{ total }} 条</span>
      </div>

      <div v-if="listErr" class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">{{ listErr }}</div></div>
      <div v-else-if="!total" class="empty"><div class="empty-icon">🗂️</div><div class="empty-text">无匹配工单</div></div>
      <table v-else>
        <thead>
          <tr>
            <th>案件ID</th><th>订单号</th><th>金额</th><th>渠道</th><th>状态</th><th>决策</th><th>创建时间</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in items" :key="c.case_id">
            <td class="mono">{{ shortId(c.case_id) }}</td>
            <td class="mono">{{ c.order_no || c.order_id }}</td>
            <td>{{ fmtMoney(c.requested_amount) }}</td>
            <td><span class="tag gray">{{ c.channel || '—' }}</span></td>
            <td><span class="tag" :class="statusTag(c.current_status)">{{ statusText(c.current_status) }}</span></td>
            <td><span class="tag" :class="decisionTag(c.final_decision)">{{ decisionText(c.final_decision) }}</span></td>
            <td class="muted">{{ fmtTime(c.created_at) }}</td>
            <td><a class="view-btn" :href="'#/cases/' + c.case_id">查看</a></td>
          </tr>
        </tbody>
      </table>
      <div v-if="total > size" class="pager">
        <button class="btn gray" :disabled="page === 1" @click="go(page - 1)">‹ 上一页</button>
        <span class="pager-info">{{ page }} / {{ totalPages }}</span>
        <button class="btn gray" :disabled="page === totalPages" @click="go(page + 1)">下一页 ›</button>
      </div>
    </div>

    <!-- 保存筛选器弹窗 -->
    <div v-if="showSave" class="modal-mask" @click.self="showSave = false">
      <div class="card modal">
        <h3>💾 保存当前筛选条件</h3>
        <label>筛选器名称</label>
        <input v-model="saveName" placeholder="如：-挂起-金额>500" />
        <div v-if="saveErr" class="error-text">{{ saveErr }}</div>
        <div style="display: flex; gap: 10px; margin-top: 16px">
          <button class="btn" :disabled="busy" @click="doSave">保存</button>
          <button class="btn gray" @click="showSave = false">取消</button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="toast">✅ {{ toast }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  deleteSavedSearch, exportCases, listSavedSearches, saveSearch, searchCases,
} from '../api'

const f = reactive({
  keyword: '', status: '', decision: '', channel: '', reason_code: '',
  amount_min: '', amount_max: '', sort_by: 'created_at', sort_order: 'desc',
})
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const listErr = ref('')
const savedFilters = ref([])
const showSave = ref(false)
const saveName = ref('')
const saveErr = ref('')
const busy = ref(false)
const toast = ref('')

const role = localStorage.getItem('role')
const canExport = ['leader', 'admin'].includes(role)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

const shortId = (id) => (id || '').slice(0, 8)
const fmtTime = (t) => (t || '').replace('T', ' ').slice(0, 19)
const fmtMoney = (n) => `¥${Number(n).toFixed(2)}`
const statusTag = (s) => ({ RUNNING: 'blue', SUSPENDED: 'yellow', COMPLETED: 'green' }[s] || 'gray')
const statusText = (s) => ({ RUNNING: '运行中', SUSPENDED: '挂起待审', COMPLETED: '已完成' }[s] || s)
const decisionTag = (d) => ({ PENDING: 'gray', APPROVED: 'green', REJECTED: 'red', SYSTEM_FAILURE: 'red' }[d] || 'gray')
const decisionText = (d) => ({ PENDING: '待决策', APPROVED: '已批准', REJECTED: '已拒绝', SYSTEM_FAILURE: '系统失败' }[d] || d)

function buildParams() {
  const p = { page: page.value, size }
  for (const k of ['keyword', 'status', 'decision', 'channel', 'reason_code', 'amount_min', 'amount_max', 'sort_by', 'sort_order']) {
    if (f[k] !== '' && f[k] != null) p[k] = f[k]
  }
  return p
}

async function doSearch() {
  loading.value = true
  listErr.value = ''
  try {
    const { data } = await searchCases(buildParams())
    items.value = data.items
    total.value = data.total
  } catch (e) {
    listErr.value = e.response?.data?.message || '检索失败'
  } finally {
    loading.value = false
  }
}

function go(p) {
  if (p < 1 || p > totalPages.value) return
  page.value = p
  doSearch()
}

function reset() {
  Object.assign(f, {
    keyword: '', status: '', decision: '', channel: '', reason_code: '',
    amount_min: '', amount_max: '', sort_by: 'created_at', sort_order: 'desc',
  })
  page.value = 1
  doSearch()
}

async function loadSaved() {
  try {
    const { data } = await listSavedSearches()
    savedFilters.value = data
  } catch { /* 忽略 */ }
}

function applySaved(s) {
  const fl = s.filters || {}
  for (const k of ['keyword', 'status', 'decision', 'channel', 'reason_code', 'amount_min', 'amount_max', 'sort_by', 'sort_order']) {
    if (k in fl) f[k] = fl[k]
  }
  page.value = 1
  doSearch()
}

function openSave() { showSave.value = true; saveName.value = ''; saveErr.value = '' }

async function doSave() {
  saveErr.value = ''
  if (!saveName.value) { saveErr.value = '请输入名称'; return }
  busy.value = true
  try {
    const filters = {}
    for (const k of ['keyword', 'status', 'decision', 'channel', 'reason_code', 'amount_min', 'amount_max', 'sort_by', 'sort_order']) {
      if (f[k] !== '' && f[k] != null) filters[k] = f[k]
    }
    await saveSearch({ name: saveName.value, filters })
    toast.value = '已保存筛选器'
    showSave.value = false
    setTimeout(() => (toast.value = ''), 2500)
    loadSaved()
  } catch (e) {
    saveErr.value = e.response?.data?.message || '保存失败'
  } finally {
    busy.value = false
  }
}

async function delSaved(id) {
  try {
    await deleteSavedSearch(id)
    loadSaved()
  } catch { /* 忽略 */ }
}

async function doExport() {
  busy.value = true
  try {
    const resp = await exportCases(buildParams())
    const url = URL.createObjectURL(new Blob([resp.data]))
    const a = document.createElement('a')
    a.href = url
    a.download = `cases_${Date.now()}.csv`
    a.click()
    URL.revokeObjectURL(url)
    toast.value = '已导出 CSV'
    setTimeout(() => (toast.value = ''), 2500)
  } catch { /* 忽略 */ } finally {
    busy.value = false
  }
}

onMounted(() => { doSearch(); loadSaved() })
</script>

<style scoped>
.search { display: flex; flex-direction: column; gap: 16px; }
.card { padding: 18px; }
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.head h3 { font-size: 15px; }
.head-actions { display: flex; gap: 8px; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--muted); }
input, select { padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; background: var(--panel2); color: var(--text); }
input.full { width: 100%; }
.actions { margin-top: 16px; }
.count { color: var(--muted); font-size: 12px; }

.saved-card { padding: 14px 18px; }
.saved-card .head { margin-bottom: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex; align-items: center; border: 1px solid var(--border);
  border-radius: 999px; overflow: hidden; background: var(--panel2);
}
.chip-btn { border: none; background: transparent; padding: 6px 10px 6px 14px; cursor: pointer; font-size: 13px; color: var(--blue); }
.chip-btn:hover { background: rgba(96, 165, 250, 0.1); }
.chip-del { border: none; background: transparent; padding: 6px 10px; cursor: pointer; color: var(--muted); font-size: 14px; }
.chip-del:hover { color: var(--red); }

.list-card { padding: 0; overflow: hidden; }
.list-card .head { padding: 15px 18px; border-bottom: 1px solid var(--border); margin-bottom: 0; background: linear-gradient(180deg, rgba(59, 130, 246, 0.06), transparent); }
table { width: 100%; border-collapse: collapse; }
thead th { text-align: left; padding: 11px 14px; color: var(--muted); font-weight: 550; font-size: 12px; letter-spacing: 0.4px; background: var(--panel2); border-bottom: 1px solid var(--border); }
tbody td { padding: 12px 14px; border-bottom: 1px solid var(--border); font-size: 13.5px; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(37, 99, 235, 0.045); }
.mono { font-family: Consolas, monospace; font-size: 13px; }
.muted { color: var(--muted); }
.view-btn { display: inline-block; padding: 4px 12px; border-radius: 999px; border: 1px solid rgba(96, 165, 250, 0.35); color: var(--blue); font-size: 12px; font-weight: 500; transition: all 0.15s; }
.view-btn:hover { background: rgba(96, 165, 250, 0.12); }
.empty { text-align: center; padding: 48px 16px; }
.empty-icon { font-size: 40px; margin-bottom: 10px; }
.empty-text { font-size: 15px; font-weight: 600; }
.pager { display: flex; align-items: center; justify-content: center; gap: 14px; padding: 14px; border-top: 1px solid var(--border); }
.pager .btn:disabled { opacity: 0.35; }
.pager-info { color: var(--muted); font-size: 13px; }
</style>