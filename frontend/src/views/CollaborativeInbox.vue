<template>
  <div class="collab">
    <!-- 待办池 -->
    <div class="card list-card">
      <div class="list-head">
        <h3>📍 班组协同台 · 待办池</h3>
        <span class="hint">仅挂起待审批单可认领/转派；SLA 超时高亮提醒</span>
      </div>

      <div v-if="queueErr" class="empty">
        <div class="empty-icon">⚠️</div>
        <div class="empty-text">{{ queueErr }}</div>
      </div>
      <div v-else-if="loading" class="empty">
        <div class="spinner"></div>
        <div class="empty-hint">加载中…</div>
      </div>
      <div v-else-if="!queue.length" class="empty">
        <div class="empty-icon">🧘</div>
        <div class="empty-text">暂无待办</div>
      </div>
      <table v-else>
        <thead>
          <tr>
            <th>案件/订单</th><th>渠道</th><th>金额</th><th>原因</th><th>SLA</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in queue" :key="c.case_id">
            <td>
              <div class="mono">{{ shortId(c.case_id) }}</div>
              <div class="muted small">{{ c.order_id }}</div>
            </td>
            <td><span class="tag gray">{{ c.channel || '—' }}</span></td>
            <td>{{ fmtMoney(c.requested_amount) }}</td>
            <td class="muted small reason">{{ c.reason || '—' }}</td>
            <td><span class="tag" :class="slaTag(c.sla)">{{ slaText(c.sla) }}</span></td>
            <td>
              <a class="view-btn" :href="'#/cases/' + c.case_id">查看</a>
              <button class="btn green small-btn" v-if="canAct" @click="onClaim(c)">认领</button>
              <button class="btn gray small-btn" v-if="canAct" @click="openTransfer(c)">转派</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 转派弹窗（需选择目标处理人） -->
    <div v-if="transferTarget" class="modal-mask" @click.self="transferTarget = null">
      <div class="card modal">
        <h3>↔️ 转派案件 {{ shortId(transferTarget.case_id) }}</h3>
        <p class="muted small" v-if="!users.length">无其他可转派用户（需先在有用户的班组）。</p>
        <label>目标处理人</label>
        <select v-model="transferUserId">
          <option v-for="u in users" :key="u.id" :value="u.id">{{ u.username }}（{{ u.role }}）</option>
        </select>
        <div v-if="transferErr" class="error-text">{{ transferErr }}</div>
        <div style="display: flex; gap: 10px; margin-top: 16px">
          <button class="btn" :disabled="busy" @click="doTransfer">转派</button>
          <button class="btn gray" @click="transferTarget = null">取消</button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="toast">✅ {{ toast }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { claimCase, getWorkspaceQueue, transferCase } from '../api'

const queue = ref([])
const loading = ref(true)
const queueErr = ref('')
const busy = ref(false)
const toast = ref('')

const transferTarget = ref(null)
const transferUserId = ref('')
const transferErr = ref('')
const users = ref([])

const role = localStorage.getItem('role')
const canAct = ['supervisor', 'leader', 'admin'].includes(role)

const shortId = (id) => (id || '').slice(0, 8)
const fmtMoney = (n) => `¥${Number(n).toFixed(2)}`
const slaTag = (s) => ({ ok: 'green', remind: 'yellow', overdue: 'red' }[s] || 'gray')
const slaText = (s) => ({ ok: '正常', remind: '将超时', overdue: '已超时' }[s] || s)

async function refresh() {
  loading.value = true
  try {
    const { data } = await getWorkspaceQueue()
    queue.value = data
    queueErr.value = ''
  } catch (e) {
    queueErr.value = e.response?.data?.message || '加载待办失败'
  } finally {
    loading.value = false
  }
}

async function onClaim(c) {
  busy.value = true
  try {
    await claimCase(c.case_id)
    toast.value = `已认领 ${shortId(c.case_id)}`
    setTimeout(() => (toast.value = ''), 2500)
    refresh()
  } catch (e) {
    queueErr.value = e.response?.data?.message || '认领失败'
  } finally {
    busy.value = false
  }
}

function openTransfer(c) {
  // 从后端拉一个简单用户列表（暂无接口，兜底：手动输入用户名由后端解析）
  transferTarget.value = c
  transferUserId.value = c.assignee_id || ''
  transferErr.value = ''
}

async function doTransfer() {
  transferErr.value = ''
  if (!transferUserId.value) { transferErr.value = '请选择目标处理人'; return }
  busy.value = true
  try {
    await transferCase(transferTarget.value.case_id, transferUserId.value)
    toast.value = `已转派 ${shortId(transferTarget.value.case_id)}`
    transferTarget.value = null
    setTimeout(() => (toast.value = ''), 2500)
    refresh()
  } catch (e) {
    transferErr.value = e.response?.data?.detail || e.response?.data?.message || '转派失败'
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.collab { display: flex; flex-direction: column; gap: 16px; }
.list-card { padding: 0; }
.list-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 15px 18px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(59, 130, 246, 0.06), transparent);
}
.list-head h3 { font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; }
.small { font-size: 12px; }
.reason { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.small-btn { padding: 4px 10px; margin-left: 6px; font-size: 12px; }
.empty { text-align: center; padding: 48px 16px; }
.empty-icon { font-size: 40px; margin-bottom: 10px; }
.empty-text { font-size: 15px; font-weight: 600; }
.empty-hint { color: var(--muted); font-size: 13px; margin-top: 6px; }
.spinner {
  width: 28px; height: 28px; margin: 8px auto 14px;
  border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 11px 14px;
  color: var(--muted); font-weight: 550; font-size: 12px; letter-spacing: 0.4px;
  background: var(--panel2); border-bottom: 1px solid var(--border);
}
tbody td { padding: 12px 14px; border-bottom: 1px solid var(--border); font-size: 13.5px; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(37, 99, 235, 0.045); }
.mono { font-family: Consolas, monospace; font-size: 13px; }
.muted { color: var(--muted); }
.view-btn {
  display: inline-block; padding: 4px 12px; border-radius: 999px;
  border: 1px solid rgba(96, 165, 250, 0.35);
  color: var(--blue); font-size: 12px; font-weight: 500;
  transition: all 0.15s;
}
.view-btn:hover { background: rgba(96, 165, 250, 0.12); }
</style>