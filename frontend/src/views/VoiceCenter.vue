<template>
  <div class="page-stack">
    <PageHeader
      eyebrow="REAL-TIME VOICE & COACHING"
      title="语音交互与客服陪练"
      description="查看实时语音容量、链路延迟预算和陪练复盘。陪练与语音测试数据保持EVAL隔离，禁止访问真实退款凭据。"
    >
      <template #actions>
        <button class="btn secondary" @click="loadData"><AppIcon name="refresh" />刷新容量</button>
      </template>
    </PageHeader>

    <div class="metric-grid">
      <div class="metric-card">
        <div class="metric-top"><span>当前会话</span><span class="metric-icon"><AppIcon name="mic" /></span></div>
        <div class="metric-value">{{ metrics.current_active ?? 0 }}<small>/{{ metrics.max_concurrent ?? '—' }}</small></div>
        <div class="metric-foot">实时并发占用 {{ pct(metrics.capacity_percent) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>首响应P99</span><span class="metric-icon orange"><AppIcon name="activity" /></span></div>
        <div class="metric-value">{{ seconds(metrics.latency_p99_seconds) }}</div>
        <div class="metric-foot">观测窗口 {{ metrics.window_minutes || 15 }} 分钟</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>错误率</span><span class="metric-icon" :class="(metrics.error_rate || 0) > 0 ? 'red' : 'green'"><AppIcon name="warning" /></span></div>
        <div class="metric-value">{{ pct(metrics.error_rate) }}</div>
        <div class="metric-foot">{{ metrics.error_count ?? 0 }} 次错误 · {{ metrics.drop_count ?? 0 }} 次丢弃</div>
      </div>
      <div class="metric-card">
        <div class="metric-top"><span>峰值并发</span><span class="metric-icon green"><AppIcon name="chart" /></span></div>
        <div class="metric-value">{{ metrics.peak_concurrency ?? '未测量' }}</div>
        <div class="metric-foot">{{ metrics.session_count ?? 0 }} 个已完成会话样本</div>
      </div>
    </div>

    <div v-if="error" class="notice danger"><AppIcon name="warning" />{{ error }}</div>

    <div class="content-grid">
      <section class="card span-7">
        <div class="card-header">
          <div><div class="card-title">实时语音链路控制台</div><div class="card-description">连接WebSocket实时通道，通过字幕信令验证STT→LLM→TTS、插话、弱网和转人工状态机。</div></div>
          <span class="status-chip" :class="wsStatusClass">{{ wsStatusText }}</span>
        </div>
        <div class="notice"><AppIcon name="shield" />该控制台使用测试信令，不创建真实订单，也不会执行退款；回复文本可由浏览器语音合成朗读。</div>
        <div class="voice-console">
          <div ref="logBox" class="event-log">
            <div v-if="!events.length" class="log-empty">连接通道后，实时事件会显示在这里。</div>
            <div v-for="(event, index) in events" :key="index" class="event-row" :class="'event-' + event.type">
              <div class="event-meta"><span>{{ event.type }}</span><time>{{ event.time }}</time></div>
              <p>{{ event.reply || event.text || event.note || event.code || event.state }}</p>
            </div>
          </div>
          <div class="voice-controls">
            <textarea v-model="transcript" rows="2" placeholder="输入模拟实时字幕，例如：我的退款为什么还没到账？" @keyup.ctrl.enter="sendTranscript"></textarea>
            <div class="control-row">
              <button v-if="wsStatus === 'idle' || wsStatus === 'closed' || wsStatus === 'error'" class="btn" @click="connectVoice"><AppIcon name="mic" />连接实时通道</button>
              <button v-else class="btn" :disabled="!transcript.trim()" @click="sendTranscript"><AppIcon name="arrow" />发送字幕</button>
              <button class="btn secondary" :disabled="wsStatus !== 'connected'" @click="interruptVoice">插话打断</button>
              <button class="btn secondary" :disabled="wsStatus !== 'connected'" @click="markWeak">弱网标记</button>
              <button class="btn red" :disabled="wsStatus !== 'connected'" @click="handoff">转人工</button>
            </div>
          </div>
        </div>
      </section>

      <section class="card span-5">
        <div class="card-header"><div><div class="card-title">链路延迟预算</div><div class="card-description">无样本时如实显示“未测量”，不伪造达标结果。</div></div></div>
        <div class="budget-list">
          <div v-for="item in pipelineItems" :key="item.key" class="budget-item">
            <div class="budget-head">
              <span>{{ item.label }}</span>
              <span class="tag" :class="item.value?.meets_budget ? 'green' : item.value?.samples ? 'red' : 'gray'">
                {{ item.value?.samples ? (item.value.meets_budget ? '达标' : '超预算') : '未测量' }}
              </span>
            </div>
            <div class="budget-track"><div :class="{ danger: item.value?.samples && !item.value?.meets_budget }" :style="{ width: budgetWidth(item.value) }"></div></div>
            <div class="budget-foot"><span>P95 {{ p95(item.value) }}</span><span>预算 ≤ {{ item.value?.budget_ms ?? '—' }}ms</span></div>
          </div>
        </div>
      </section>

      <section class="card span-5">
        <div class="card-header"><div><div class="card-title">陪练场景</div><div class="card-description">选择场景、难度和客户情绪，创建隔离的训练会话。</div></div></div>
        <label>训练场景</label>
        <select v-model="coachForm.scene_id">
          <option v-for="scene in scenarios" :key="scene.scene_id" :value="scene.scene_id">{{ scene.name }}</option>
        </select>
        <div class="scene-description">{{ activeScene?.description || '加载场景目录中…' }}</div>
        <div class="field-row">
          <div><label>难度</label><select v-model="coachForm.difficulty"><option v-for="item in activeScene?.difficulties || defaults.difficulties" :key="item" :value="item">{{ difficultyText(item) }}</option></select></div>
          <div><label>客户情绪</label><select v-model="coachForm.emotion"><option v-for="item in activeScene?.emotions || defaults.emotions" :key="item" :value="item">{{ emotionText(item) }}</option></select></div>
        </div>
        <button class="btn coach-start" :disabled="coachBusy || !coachForm.scene_id" @click="startCoaching">{{ coachBusy ? '创建中…' : '开始陪练' }}</button>
        <div v-if="coachSession" class="session-banner">
          <div><span>训练会话</span><strong>{{ coachSession.coaching_session_id }}</strong></div>
          <span class="tag purple">{{ coachSession.scene_name }}</span>
        </div>
      </section>

      <section class="card span-7">
        <div class="card-header"><div><div class="card-title">话术评分与复盘</div><div class="card-description">评分覆盖专业、情绪、合规、结案和效率五个维度，原文在落库前脱敏。</div></div></div>
        <div v-if="!coachSession" class="empty-state"><div><AppIcon name="users" /><strong>请先创建陪练会话</strong><p>完成场景选择后即可录入客户问题和客服回复。</p></div></div>
        <template v-else>
          <div v-for="(turn, index) in turns" :key="index" class="turn-editor">
            <span class="turn-index">{{ index + 1 }}</span>
            <div><label>客户问题</label><input v-model="turn.prompt" placeholder="客户提出的问题或异议" /></div>
            <div><label>客服回复</label><textarea v-model="turn.reply" rows="2" placeholder="输入你的处理话术"></textarea></div>
            <button v-if="turns.length > 1" class="remove-turn" title="删除回合" @click="turns.splice(index, 1)">×</button>
          </div>
          <div class="score-actions">
            <button class="btn secondary" :disabled="turns.length >= (coachSession.max_turns || 10)" @click="turns.push({ prompt: '', reply: '' })"><AppIcon name="plus" />增加回合</button>
            <button class="btn" :disabled="coachBusy || !validTurns" @click="scoreCoaching"><AppIcon name="evaluation" />生成评分</button>
          </div>
          <div v-if="coachScore" class="score-result">
            <div class="total-score"><span>综合得分</span><strong>{{ coachScore.total }}</strong><small>/100</small></div>
            <div class="dimension-grid"><div v-for="(score, name) in coachScore.score || {}" :key="name"><span>{{ name }}</span><strong>{{ score }}</strong></div></div>
            <div class="suggestion"><strong>改进建议</strong><p>{{ coachScore.suggestion }}</p></div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import { apiWebSocketUrl, voiceGetMetrics, voiceListScenarios, voiceScoreCoaching, voiceStartCoaching } from '../api'

const metrics = ref({})
const scenarios = ref([])
const error = ref('')
const wsStatus = ref('idle')
const ws = ref(null)
const events = ref([])
const transcript = ref('')
const logBox = ref(null)
const coachBusy = ref(false)
const coachSession = ref(null)
const coachScore = ref(null)
const turns = ref([{ prompt: '', reply: '' }])
const defaults = { difficulties: ['easy', 'medium', 'hard'], emotions: ['calm', 'anxious', 'angry'] }
const coachForm = reactive({ scene_id: '', difficulty: 'medium', emotion: 'calm' })

const activeScene = computed(() => scenarios.value.find((x) => x.scene_id === coachForm.scene_id))
const validTurns = computed(() => turns.value.some((x) => x.reply.trim()))
const wsStatusText = computed(() => ({ idle: '未连接', connecting: '连接中', connected: '实时在线', closed: '已断开', error: '连接异常' }[wsStatus.value]))
const wsStatusClass = computed(() => ({ connected: 'green', connecting: 'yellow', error: 'red', closed: 'gray', idle: 'gray' }[wsStatus.value]))
const pipelineItems = computed(() => {
  const data = metrics.value.pipeline_budget || {}
  const labels = { VAD_END: 'VAD端点检测', STT: '流式STT', LLM_FIRST: 'LLM首Token', TTS_FIRST: 'TTS首包' }
  return Object.entries(labels).map(([key, label]) => ({ key, label, value: data[key] }))
})

function messageOf(e) {
  const detail = e.response?.data?.detail || e.response?.data?.message
  return typeof detail === 'string' ? detail : detail?.message || '请求失败，请稍后重试'
}

async function loadData() {
  error.value = ''
  try {
    const [metricRes, sceneRes] = await Promise.all([voiceGetMetrics(), voiceListScenarios()])
    metrics.value = metricRes.data || {}
    scenarios.value = sceneRes.data.scenarios || []
    if (!coachForm.scene_id && scenarios.value.length) coachForm.scene_id = scenarios.value[0].scene_id
  } catch (e) {
    error.value = messageOf(e)
  }
}

function connectVoice() {
  closeSocket()
  wsStatus.value = 'connecting'
  events.value = []
  const token = encodeURIComponent(localStorage.getItem('token') || '')
  const socket = new WebSocket(apiWebSocketUrl('/voice/ws?token=' + token))
  ws.value = socket
  socket.onopen = () => { wsStatus.value = 'connected' }
  socket.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data)
      events.value.push({ ...event, time: new Date().toLocaleTimeString() })
      if (event.reply && 'speechSynthesis' in window) {
        window.speechSynthesis.cancel()
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(event.reply))
      }
      nextTick(() => { if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight })
    } catch {
      events.value.push({ type: 'raw', text: message.data, time: new Date().toLocaleTimeString() })
    }
  }
  socket.onerror = () => { wsStatus.value = 'error'; error.value = '实时语音通道连接失败，请确认后端服务和网关配置。' }
  socket.onclose = () => { if (wsStatus.value !== 'error') wsStatus.value = 'closed'; ws.value = null }
}

function sendControl(payload) {
  if (ws.value?.readyState !== WebSocket.OPEN) return
  ws.value.send(JSON.stringify(payload))
}

function sendTranscript() {
  if (!transcript.value.trim()) return
  sendControl({ type: 'stt', text: transcript.value.trim() })
  transcript.value = ''
}

function interruptVoice() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel()
  sendControl({ type: 'interrupt' })
}
function markWeak() { sendControl({ type: 'weak_signal' }) }
function handoff() { sendControl({ type: 'handoff', reason: '前端控制台请求人工接管' }) }
function closeSocket() {
  if (ws.value && ws.value.readyState === WebSocket.OPEN) {
    sendControl({ type: 'stop' })
    ws.value.close()
  }
  ws.value = null
}

async function startCoaching() {
  coachBusy.value = true
  error.value = ''
  try {
    coachSession.value = (await voiceStartCoaching(coachForm.scene_id, coachForm.difficulty, coachForm.emotion)).data
    coachScore.value = null
    turns.value = [{ prompt: '', reply: '' }]
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    coachBusy.value = false
  }
}

async function scoreCoaching() {
  coachBusy.value = true
  error.value = ''
  try {
    const cleanTurns = turns.value.filter((x) => x.reply.trim())
    coachScore.value = (await voiceScoreCoaching(coachSession.value.coaching_session_id, cleanTurns)).data
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    coachBusy.value = false
  }
}

const pct = (value) => value == null ? '未测量' : (Number(value) * 100).toFixed(1) + '%'
const seconds = (value) => value == null ? '未测量' : (Number(value) * 1000).toFixed(0) + 'ms'
const p95 = (value) => value?.p95_seconds == null ? '未测量' : (Number(value.p95_seconds) * 1000).toFixed(0) + 'ms'
const budgetWidth = (value) => {
  if (!value?.samples || value.p95_seconds == null || !value.budget_ms) return '0%'
  return Math.min(100, Number(value.p95_seconds) * 1000 / value.budget_ms * 100) + '%'
}
const difficultyText = (value) => ({ easy: '简单', medium: '中等', hard: '困难' }[value] || value)
const emotionText = (value) => ({ calm: '平静', anxious: '焦虑', angry: '愤怒' }[value] || value)

onMounted(loadData)
onUnmounted(() => { closeSocket(); if ('speechSynthesis' in window) window.speechSynthesis.cancel() })
</script>

<style scoped>
.metric-value small { margin-left: 3px; color: var(--muted); font-size: 14px; font-weight: 600; }
.voice-console { margin-top: 15px; overflow: hidden; border: 1px solid var(--border); border-radius: 12px; }
.event-log { height: 300px; padding: 14px; overflow: auto; background: #0f1929; color: #dce7f7; }
.log-empty { height: 100%; display: grid; place-items: center; color: #7386a3; font-size: 12px; }
.event-row { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,.06); }
.event-meta { display: flex; justify-content: space-between; color: #7692bd; font-size: 10px; text-transform: uppercase; }
.event-row p { margin-top: 3px; color: #d9e5f6; font-size: 13px; }
.event-error p { color: #ff9ca4; }
.event-TTS_SPEAKING p { color: #9fc0ff; }
.voice-controls { padding: 12px; background: #f7f9fc; }
.control-row { display: flex; gap: 7px; margin-top: 9px; flex-wrap: wrap; }
.budget-list { display: flex; flex-direction: column; gap: 17px; }
.budget-head, .budget-foot { display: flex; align-items: center; justify-content: space-between; }
.budget-head > span:first-child { font-weight: 620; }
.budget-track { height: 7px; margin: 8px 0 5px; overflow: hidden; background: #edf1f6; border-radius: 8px; }
.budget-track div { height: 100%; background: linear-gradient(90deg, #2f6beb, #6b8ef7); border-radius: 8px; }
.budget-track div.danger { background: linear-gradient(90deg, #e4515b, #c42a35); }
.budget-foot { color: var(--muted); font-size: 10px; }
.scene-description { min-height: 48px; margin-top: 9px; padding: 10px; color: var(--muted); background: var(--panel2); border-radius: 9px; font-size: 12px; }
.coach-start { width: 100%; margin-top: 15px; }
.session-banner { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 13px; padding: 10px 12px; background: #f4f0ff; border: 1px solid #e5dcff; border-radius: 10px; }
.session-banner div { display: flex; flex-direction: column; min-width: 0; }
.session-banner span:first-child { color: var(--muted); font-size: 10px; }
.session-banner strong { overflow: hidden; text-overflow: ellipsis; font-size: 12px; }
.turn-editor { position: relative; display: grid; grid-template-columns: 28px 1fr 1.5fr 24px; align-items: end; gap: 9px; padding: 11px; margin-bottom: 9px; background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; }
.turn-editor label { margin-top: 0; }
.turn-index { align-self: center; width: 24px; height: 24px; display: grid; place-items: center; color: var(--blue); background: #eaf1ff; border-radius: 7px; font-size: 11px; font-weight: 700; }
.remove-turn { align-self: center; color: var(--red); background: transparent; border: 0; cursor: pointer; font-size: 20px; }
.score-actions { display: flex; justify-content: space-between; gap: 10px; margin-top: 12px; }
.score-result { display: grid; grid-template-columns: 110px 1fr; gap: 14px; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }
.total-score { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 14px; background: linear-gradient(145deg, #edf4ff, #f5f1ff); border-radius: 12px; }
.total-score span { color: var(--muted); font-size: 11px; }
.total-score strong { color: var(--blue); font-size: 38px; line-height: 1.1; }
.total-score small { color: var(--muted); }
.dimension-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 7px; }
.dimension-grid div { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 9px; background: var(--panel2); border-radius: 9px; }
.dimension-grid span { color: var(--muted); font-size: 10px; }
.dimension-grid strong { margin-top: 4px; font-size: 17px; }
.suggestion { grid-column: 1 / -1; padding: 12px; background: #f7f9fc; border-radius: 10px; }
.suggestion p { margin-top: 4px; color: var(--muted); font-size: 12px; }
@media (max-width: 760px) {
  .turn-editor { grid-template-columns: 28px 1fr 24px; }
  .turn-editor > div:last-of-type { grid-column: 2 / 3; }
  .dimension-grid { grid-template-columns: repeat(3, 1fr); }
  .score-result { grid-template-columns: 1fr; }
}
</style>
