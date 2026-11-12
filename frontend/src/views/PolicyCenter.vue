<template>
  <div class="policy">
    <div v-if="loadError" class="card load-error" role="alert">
      <span>{{ loadError }}</span>
      <button class="btn gray" @click="refresh">重新加载</button>
    </div>
    <!-- 生效策略一览（含回退到默认档） -->
    <div class="card briefing-card">
      <div class="briefing-head">
        <h3>📋 生效策略预览 Effective Preview</h3>
        <span class="hint">按渠道显示绑定档生效阈值；未绑定渠道回退到默认档</span>
      </div>
      <table>
        <thead>
          <tr><th>渠道</th><th>生效金额阈值</th><th>OCR置信度</th><th>欺诈放行分</th><th>欺诈挂起分</th><th>黑名单自动拒绝</th><th>SLA提醒(h)</th><th>SLA升级(h)</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="8" class="muted center">加载中…</td></tr>
          <tr v-else-if="!previews.length"><td colspan="8" class="muted center">暂无可用的生效策略</td></tr>
          <tr v-for="e in previews" :key="e.channel">
            <td><span class="tag blue">{{ e.channel }}</span></td>
            <td>{{ fmtNum(e.amount_threshold) }}</td>
            <td>{{ Math.round((e.ocr_confidence || 0) * 100) }}%</td>
            <td>{{ e.fraud_pass }}</td>
            <td>{{ e.fraud_suspend }}</td>
            <td><span class="tag" :class="e.blacklist_autoreject ? 'red' : 'gray'">{{ e.blacklist_autoreject ? '是' : '否' }}</span></td>
            <td>{{ e.sla_remind_hr }}</td>
            <td>{{ e.sla_escalate_hr }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 策略档管理 -->
    <div class="card list-card">
      <div class="list-head">
        <h3>🗂️ 策略档 Profiles</h3>
        <span class="hint">admin 可编辑；阈值修改不破坏三态/HITL，仅影响「秒退/挂起」判定</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>名称</th><th>金额阈值(¥)</th><th>OCR置信度</th><th>欺诈放行分</th><th>欺诈挂起分</th><th>黑名单自动拒</th><th>SLA提醒(h)</th><th>SLA升级(h)</th><th>默认档</th><th v-if="canEdit"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="10" class="muted center">加载中…</td></tr>
          <tr v-else-if="!profiles.length"><td colspan="10" class="muted center">暂无策略档，请创建</td></tr>
          <tr v-for="p in profiles" :key="p.id">
            <td><span class="tag" :class="p.is_default ? 'blue' : 'gray'">{{ p.name }}</span></td>
            <td>{{ fmtNum(p.amount_threshold) }}</td>
            <td>{{ Math.round((p.ocr_confidence || 0) * 100) }}%</td>
            <td>{{ p.fraud_pass }}</td>
            <td>{{ p.fraud_suspend }}</td>
            <td><span class="tag" :class="p.blacklist_autoreject ? 'red' : 'gray'">{{ p.blacklist_autoreject ? '是' : '否' }}</span></td>
            <td>{{ p.sla_remind_hr }}</td>
            <td>{{ p.sla_escalate_hr }}</td>
            <td><span class="tag" :class="p.is_default ? 'green' : 'muted'">{{ p.is_default ? '默认' : '—' }}</span></td>
            <td v-if="canEdit"><button class="btn gray" @click="openEdit(p)">编辑</button></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!canEdit" class="perm-hint">⚠️ 仅 admin 角色可编辑策略</div>
    </div>

    <!-- 渠道绑定 -->
    <div class="card list-card">
      <div class="list-head">
        <h3>🔗 渠道绑定 Bindings</h3>
        <span class="hint">为每个渠道指定生效档；未绑定的渠道回退到默认档</span>
      </div>
      <table>
        <thead>
          <tr><th>渠道</th><th>绑定档</th><th>生效预览</th><th v-if="canEdit"></th></tr>
        </thead>
        <tbody>
          <tr v-for="ch in channels" :key="ch">
            <td>{{ ch }}</td>
            <td><span class="tag blue">{{ profileName(bindingOf(ch)) || '（默认）' }}</span></td>
            <td class="muted mono">{{ effSummary(ch) }}</td>
            <td v-if="canEdit"><button class="btn gray" @click="openBind(ch)">重绑</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 编辑弹窗 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="card modal">
        <h3>✏️ 编辑策略：{{ editing.name }}</h3>
        <label>金额阈值（¥，超过转人工）</label>
        <input v-model.number="editForm.amount_threshold" type="number" step="1" min="1" />
        <label>OCR 置信度（0–1，低于转人工）</label>
        <input v-model.number="editForm.ocr_confidence" type="number" step="0.05" min="0" max="1" />
        <label>欺诈放行分（0–100）</label>
        <input v-model.number="editForm.fraud_pass" type="number" min="0" max="100" />
        <label>欺诈挂起分（0–100）</label>
        <input v-model.number="editForm.fraud_suspend" type="number" min="0" max="100" />
        <label>SLA 提醒（小时）</label>
        <input v-model.number="editForm.sla_remind_hr" type="number" min="1" max="168" />
        <label>SLA 升级（小时）</label>
        <input v-model.number="editForm.sla_escalate_hr" type="number" min="1" max="168" />
        <label class="check">
          <input type="checkbox" v-model="editForm.blacklist_autoreject" /> 黑名单自动拒绝
        </label>
        <div v-if="editErr" class="error-text">{{ editErr }}</div>
        <div style="display: flex; gap: 10px; margin-top: 16px">
          <button class="btn" :disabled="saving" @click="saveEdit">{{ saving ? '保存中…' : '保存' }}</button>
          <button class="btn gray" @click="editing = null">取消</button>
        </div>
      </div>
    </div>

    <!-- 重绑弹窗 -->
    <div v-if="binding" class="modal-mask" @click.self="binding = null">
      <div class="card modal">
        <h3>🔗 绑定渠道：{{ binding }}</h3>
        <label>选择策略</label>
        <select v-model="bindProfileId">
          <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}<template v-if="p.is_default">（默认）</template></option>
        </select>
        <div v-if="bindErr" class="error-text">{{ bindErr }}</div>
        <div style="display: flex; gap: 10px; margin-top: 16px">
          <button class="btn" :disabled="saving" @click="saveBind">{{ saving ? '保存中…' : '保存' }}</button>
          <button class="btn gray" @click="binding = null">取消</button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="toast">✅ {{ toast }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  bindPolicyChannel, getEffectivePolicy, getPolicyBindings, getPolicyProfiles, updatePolicyProfile,
} from '../api'

const profiles = ref([])
const channels = ref([])
const bindingMap = reactive({})   // channel -> {profile_id, effective}
const loading = ref(true)
const editing = ref(null)
const binding = ref(null)        // 当前重绑渠道
const bindProfileId = ref('')
const editForm = reactive({})
const saving = ref(false)
const editErr = ref('')
const bindErr = ref('')
const toast = ref('')
const loadError = ref('')

const role = localStorage.getItem('role')
const canEdit = role === 'admin'
const previews = computed(() => channels.value
  .map((channel) => ({ channel, ...(bindingMap[channel]?.effective || {}) }))
  .filter((item) => Object.keys(item).length > 1))

function profileName(profileId) {
  if (!profileId) return ''
  const p = profiles.value.find((x) => x.id === profileId)
  return p ? p.name : ''
}
const bindingOf = (ch) => bindingMap[ch]?.profile_name || bindingMap[ch]?.profile_id || ''

function effSummary(ch) {
  const e = bindingMap[ch]?.effective
  if (!e) return ''
  return `¥${fmtNum(e.amount_threshold)} · OCR ${Math.round((e.ocr_confidence || 0) * 100)}% · 挂起${e.fraud_suspend}`
}

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    const [prof, binds] = await Promise.all([getPolicyProfiles(), getPolicyBindings()])
    profiles.value = prof.data.profiles
    channels.value = binds.data.channels
    for (const ch of Object.keys(bindingMap)) delete bindingMap[ch]
    for (const ch of binds.data.channels) bindingMap[ch] = { profile_id: '', effective: null }
    for (const b of binds.data.bindings) {
      if (b.channel in bindingMap) bindingMap[b.channel].profile_id = b.profile_id
    }
    // 预览每渠道生效
    const previewResults = await Promise.allSettled(
      channels.value.map(async (ch) => {
        const r = await getEffectivePolicy(ch)
        bindingMap[ch].effective = r.data.effective
      })
    )
    const failedCount = previewResults.filter((item) => item.status === 'rejected').length
    if (failedCount) loadError.value = `${failedCount} 个渠道的生效策略加载失败，已保留其余可用数据。`
  } catch (e) {
    const detail = e.response?.data?.detail
    loadError.value = e.response?.status === 403
      ? '当前账号没有读取策略中心的权限，请使用治理负责人或管理员账号。'
      : (typeof detail === 'string' ? detail : '策略中心加载失败，请检查后端服务后重试。')
  } finally {
    loading.value = false
  }
}

function openEdit(p) {
  editing.value = p
  Object.assign(editForm, {
    amount_threshold: p.amount_threshold,
    ocr_confidence: p.ocr_confidence,
    fraud_pass: p.fraud_pass,
    fraud_suspend: p.fraud_suspend,
    blacklist_autoreject: p.blacklist_autoreject,
    sla_remind_hr: p.sla_remind_hr,
    sla_escalate_hr: p.sla_escalate_hr,
  })
  editErr.value = ''
}

async function saveEdit() {
  editErr.value = ''
  saving.value = true
  try {
    await updatePolicyProfile(editing.value.id, { ...editForm })
    toast.value = `已更新策略「${editing.value.name}」`
    editing.value = null
    refresh()
    setTimeout(() => (toast.value = ''), 3000)
  } catch (e) {
    editErr.value = e.response?.data?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

function openBind(ch) {
  binding.value = ch
  bindProfileId.value = bindingMap[ch]?.profile_id || profiles.value[0]?.id || ''
  bindErr.value = ''
}

async function saveBind() {
  bindErr.value = ''
  saving.value = true
  try {
    await bindPolicyChannel({ channel: binding.value, profile_id: bindProfileId.value })
    toast.value = `渠道「${binding.value}」已绑定`
    binding.value = null
    refresh()
    setTimeout(() => (toast.value = ''), 3000)
  } catch (e) {
    bindErr.value = e.response?.data?.message || '绑定失败'
  } finally {
    saving.value = false
  }
}

const fmtNum = (n) => (n == null ? '—' : Number(n).toLocaleString('zh-CN'))

onMounted(refresh)
</script>

<style scoped>
.policy { display: flex; flex-direction: column; gap: 16px; }
.list-card { padding: 0; overflow: hidden; }
.list-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 15px 18px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(59, 130, 246, 0.06), transparent);
}
.list-head h3 { font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; }
.perm-hint {
  padding: 10px 18px; color: var(--yellow); font-size: 12.5px;
  background: rgba(251, 191, 36, 0.06);
}
.load-error {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  color: var(--red); border-color: rgba(248, 113, 113, 0.35);
}
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 11px 14px;
  color: var(--muted); font-weight: 550; font-size: 12px; letter-spacing: 0.4px;
  background: var(--panel2); border-bottom: 1px solid var(--border);
}
tbody td { padding: 12px 14px; border-bottom: 1px solid var(--border); font-size: 13.5px; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(37, 99, 235, 0.045); }
.mono { font-family: Consolas, monospace; font-size: 12.5px; }
.muted { color: var(--muted); }
.center { text-align: center; padding: 32px 16px; }
.check { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
</style>
