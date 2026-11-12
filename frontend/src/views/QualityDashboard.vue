<template>
  <div class="quality-page">
    <!-- 顶栏 -->
    <div class="quality-header">
      <div class="header-left">
        <h2>📊 质量与效率看板</h2>
        <span class="header-badge" :class="online ? 'badge-online' : 'badge-offline'">
          {{ online ? '● 在线' : '○ 离线' }}
        </span>
      </div>
      <div class="header-right">
        <span class="meta-item">🔄 {{ refreshInterval }}s 自动刷新</span>
        <button class="btn gray" style="padding: 4px 12px; font-size: 12px" @click="refresh">🔄 刷新</button>
        <a class="back-link" href="#/">← 工作台</a>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="loading-box">
      <div class="spinner"></div>
      <div class="loading-text">加载看板数据…</div>
    </div>

    <template v-else>
      <!-- 质量 KPI 行 -->
      <div class="kpi-row">
        <div class="kpi-card blue">
          <div class="kpi-icon">✅</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ decision_dist.APPROVED }}</div>
            <div class="kpi-label">自动秒退（占比 {{ pct(decision_dist.APPROVED, total) }}%）</div>
          </div>
          <div class="kpi-sub">累计 {{ total }} 单</div>
        </div>
        <div class="kpi-card yellow">
          <div class="kpi-icon">⏸️</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ status_dist.SUSPENDED }}</div>
            <div class="kpi-label">挂起待审</div>
          </div>
          <div class="kpi-sub">需人工介入 {{ pct(status_dist.SUSPENDED, total) }}%</div>
        </div>
        <div class="kpi-card red">
          <div class="kpi-icon">🔴</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ decision_dist.SYSTEM_FAILURE }}</div>
            <div class="kpi-label">系统失败</div>
          </div>
          <div class="kpi-sub">需人工处理</div>
        </div>
        <div class="kpi-card green">
          <div class="kpi-icon">⏱️</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ avgAgentMs }}<span class="unit">ms</span></div>
            <div class="kpi-label">Agent 平均耗时</div>
          </div>
          <div class="kpi-sub">整体处理效率</div>
        </div>
      </div>

      <!-- 工单趋势 + 决策分布 -->
      <div class="chart-row">
        <div class="chart-card wide">
          <div class="chart-title-row">
            <h4>📈 工单趋势</h4>
            <div class="tab-group">
              <button class="tab-btn" :class="{ active: curTrend === 'hourly' }" @click="curTrend='hourly'">近 24 小时</button>
              <button class="tab-btn" :class="{ active: curTrend === 'daily' }" @click="curTrend='daily'">近 7 天</button>
            </div>
          </div>
          <div ref="trendChart" class="chart-box" style="height: 220px; width: 100%"></div>
        </div>
        <div class="chart-card">
          <h4>决策质量分布</h4>
          <div class="chart-container">
            <div ref="decisionChart" class="chart-box"></div>
            <div class="chart-legend">
              <div class="legend-item"><span class="dot green"></span>已批准 {{ decision_dist.APPROVED }}</div>
              <div class="legend-item"><span class="dot red"></span>已拒绝 {{ decision_dist.REJECTED }}</div>
              <div class="legend-item"><span class="dot gray"></span>待决策 {{ decision_dist.PENDING }}</div>
              <div class="legend-item"><span class="dot red" style="background:#7f1d1d"></span>系统失败 {{ decision_dist.SYSTEM_FAILURE }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 效率双图：Agent 耗时 + 成功率 -->
      <div class="chart-row">
        <div class="chart-card">
          <h4>⏱️ Agent 平均耗时（毫秒）</h4>
          <div ref="agentChart" class="chart-box" style="height: 220px; width: 100%"></div>
        </div>
        <div class="chart-card">
          <h4>✅ Agent 成功率</h4>
          <div ref="successChart" class="chart-box" style="height: 220px; width: 100%"></div>
        </div>
        <div class="chart-card">
          <h4>🎯 决策模式</h4>
          <div class="chart-container">
            <div ref="modeChart" class="chart-box"></div>
            <div class="chart-legend">
              <div class="legend-item"><span class="dot blue"></span>自动秒退 {{ auto_vs_manual.auto }}</div>
              <div class="legend-item"><span class="dot yellow"></span>人工决策 {{ auto_vs_manual.manual }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 风险分布 -->
      <div class="card full-width">
        <div class="chart-title-row">
          <h4>⚠️ 风险等级分布</h4>
          <span class="meta-item">按欺诈分数 60/20 分档</span>
        </div>
        <div class="risk-bars">
          <div class="risk-item">
            <div class="risk-label">高风险 (≥60)</div>
            <div class="risk-track"><div class="risk-fill red" :style="{ width: riskPct(risk_dist.HIGH) + '%' }"></div></div>
            <div class="risk-val">{{ risk_dist.HIGH }} 单 · {{ riskPct(risk_dist.HIGH) }}%</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">中风险 (21–59)</div>
            <div class="risk-track"><div class="risk-fill yellow" :style="{ width: riskPct(risk_dist.MEDIUM) + '%' }"></div></div>
            <div class="risk-val">{{ risk_dist.MEDIUM }} 单 · {{ riskPct(risk_dist.MEDIUM) }}%</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">低风险 (≤20)</div>
            <div class="risk-track"><div class="risk-fill green" :style="{ width: riskPct(risk_dist.LOW) + '%' }"></div></div>
            <div class="risk-val">{{ risk_dist.LOW }} 单 · {{ riskPct(risk_dist.LOW) }}%</div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, nextTick, watch } from 'vue'
import { default as axios } from 'axios'
import * as echarts from 'echarts'

// 看板复用监控大盘数据：Agent 耗时/成功率、决策分布、风险分布、趋势
const api = axios.create({ baseURL: '/api/v1', timeout: 20000 })
api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem('token')
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

const data = ref({
  total: 0, today_count: 0, yesterday_count: 0,
  status_dist: { RUNNING: 0, SUSPENDED: 0, COMPLETED: 0 },
  decision_dist: { APPROVED: 0, REJECTED: 0, SYSTEM_FAILURE: 0, PENDING: 0 },
  risk_dist: { HIGH: 0, MEDIUM: 0, LOW: 0 },
  agent_avg_ms: {},
  agent_success_pct: {},
  auto_vs_manual: { auto: 0, manual: 0 },
  recent_cases: [],
  trend: { hourly: { labels: [], counts: [] }, daily: { labels: [], counts: [] } },
  refresh_interval: 5,
})
const loading = ref(true)
const online = ref(true)
const refreshInterval = ref(5)
const refreshTimer = ref(null)
const curTrend = ref('hourly')

const trendChart = ref(null)
const decisionChart = ref(null)
const agentChart = ref(null)
const successChart = ref(null)
const modeChart = ref(null)

const decision_dist = computed(() => data.value.decision_dist)
const status_dist = computed(() => data.value.status_dist)
const risk_dist = computed(() => data.value.risk_dist)
const auto_vs_manual = computed(() => data.value.auto_vs_manual)
const total = computed(() => data.value.total)

const avgAgentMs = computed(() => {
  const vals = Object.values(data.value.agent_avg_ms || {})
  if (!vals.length) return 0
  return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length)
})

const pct = (n, t) => t ? Number(((n / t) * 100).toFixed(1)) : 0
const riskPct = (n) => pct(n, total.value)

function disposeCharts() { charts.forEach(c => { try { c.dispose() } catch {} }); charts = [] }
let charts = []

function renderCharts() {
  disposeCharts()
  const ec = echarts

  // 趋势图
  const tChart = ec.init(trendChart.value)
  const trendData = curTrend.value === 'hourly' ? data.value.trend.hourly : data.value.trend.daily
  tChart.setOption({
    tooltip: { trigger: 'axis', formatter: (p) => `${p[0].name}<br/>工单数: ${p[0].value}` },
    grid: { left: 45, right: 20, top: 15, bottom: 25 },
    xAxis: { type: 'category', data: trendData.labels, axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', minInterval: 1, name: '工单数', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'line', smooth: true, showSymbol: true, symbolSize: 6,
      lineStyle: { color: '#3b82f6', width: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: 'rgba(59, 130, 246, 0.3)' }, { offset: 1, color: 'rgba(59, 130, 246, 0.02)' },
      ]}},
      data: trendData.counts,
    }],
  })
  charts.push(tChart)

  // 决策分布
  const dChart = ec.init(decisionChart.value)
  dChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['45%', '70%'], avoidLabelOverlap: true, label: { show: false },
      data: [
        { value: data.value.decision_dist.APPROVED, name: '已批准', itemStyle: { color: '#059669' } },
        { value: data.value.decision_dist.REJECTED, name: '已拒绝', itemStyle: { color: '#dc2626' } },
        { value: data.value.decision_dist.PENDING, name: '待决策', itemStyle: { color: '#94a3b8' } },
        { value: data.value.decision_dist.SYSTEM_FAILURE, name: '系统失败', itemStyle: { color: '#7f1d1d' } },
      ],
    }],
  })
  charts.push(dChart)

  // Agent 耗时
  const agentNames = { intake: '受理', ocr: 'OCR识别', fraud: '欺诈风控', sentiment: '舆情分析', amount_decision: '金额决策', refund_execution: '退款执行' }
  const agents = Object.keys(agentNames)
  const aChart = ec.init(agentChart.value)
  aChart.setOption({
    tooltip: { trigger: 'axis', formatter: (p) => `${p[0].name}<br/>平均耗时: ${p[0].value}ms` },
    grid: { left: 50, right: 20, top: 20, bottom: 35 },
    xAxis: { type: 'category', data: agents.map(a => agentNames[a] || a), axisLabel: { rotate: 15, fontSize: 11 } },
    yAxis: { type: 'value', name: 'ms', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar', barWidth: 22,
      data: agents.map(a => data.value.agent_avg_ms[a] || 0),
      itemStyle: {
        borderRadius: [4, 4, 0, 0],
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
          { offset: 0, color: '#3b82f6' }, { offset: 1, color: '#1d4ed8' },
        ]},
      },
    }],
  })
  charts.push(aChart)

  // Agent 成功率
  const sChart = ec.init(successChart.value)
  sChart.setOption({
    tooltip: { trigger: 'axis', formatter: (p) => `${p[0].name}<br/>成功率: ${p[0].value}%` },
    grid: { left: 50, right: 20, top: 20, bottom: 35 },
    xAxis: { type: 'category', data: agents.map(a => agentNames[a] || a), axisLabel: { rotate: 15, fontSize: 11 } },
    yAxis: { type: 'value', min: 90, max: 100, name: '%', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar', barWidth: 22,
      data: agents.map(a => data.value.agent_success_pct[a] || 100),
      itemStyle: {
        borderRadius: [4, 4, 0, 0],
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
          { offset: 0, color: '#10b981' }, { offset: 1, color: '#059669' },
        ]},
      },
    }],
  })
  charts.push(sChart)

  // 自动 vs 人工
  const mChart = ec.init(modeChart.value)
  mChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['45%', '70%'], avoidLabelOverlap: true, label: { show: false },
      data: [
        { value: data.value.auto_vs_manual.auto, name: '自动秒退', itemStyle: { color: '#2563eb' } },
        { value: data.value.auto_vs_manual.manual, name: '人工决策', itemStyle: { color: '#b45309' } },
      ],
    }],
  })
  charts.push(mChart)
}

async function refresh() {
  try {
    const res = await api.get('/dashboard/monitor')
    data.value = res.data
    refreshInterval.value = res.data.refresh_interval || 5
    online.value = true
    await nextTick()
    renderCharts()
  } catch (e) {
    online.value = false
    console.error('看板数据加载失败', e)
  } finally {
    loading.value = false
  }
}

watch(curTrend, () => { nextTick(() => renderCharts()) })

onMounted(() => {
  refresh()
  refreshTimer.value = setInterval(refresh, refreshInterval.value * 1000)
})

onUnmounted(() => {
  if (refreshTimer.value) clearInterval(refreshTimer.value)
  disposeCharts()
})
</script>

<style scoped>
.quality-page { display: flex; flex-direction: column; gap: 14px; }

.quality-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 12px 18px; box-shadow: var(--shadow-sm);
}
.header-left { display: flex; align-items: center; gap: 10px; }
.quality-header h2 { font-size: 17px; letter-spacing: 0.5px; margin: 0; }
.header-badge { font-size: 11px; padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge-online { background: rgba(5, 150, 105, 0.12); color: var(--green); border: 1px solid rgba(5, 150, 105, 0.25); }
.badge-offline { background: rgba(220, 38, 38, 0.12); color: var(--red); border: 1px solid rgba(220, 38, 38, 0.25); }
.header-right { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-left: auto; align-items: center; }
.back-link { font-size: 12px; color: var(--blue); text-decoration: none; padding: 4px 10px; border: 1px solid rgba(96,165,250,0.3); border-radius: 6px; }
.back-link:hover { background: rgba(96,165,250,0.1); }

.loading-box { text-align: center; padding: 80px 16px; }
.loading-text { color: var(--muted); margin-top: 12px; }
.spinner { width: 32px; height: 32px; margin: 0 auto; border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 质量 KPI */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.kpi-card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; display: flex; align-items: center; gap: 14px;
  position: relative; overflow: hidden; box-shadow: var(--shadow-sm);
}
.kpi-card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }
.kpi-card.blue::before { background: var(--blue); box-shadow: 0 0 12px rgba(96,165,250,0.7); }
.kpi-card.green::before { background: var(--green); box-shadow: 0 0 12px rgba(52,211,153,0.7); }
.kpi-card.yellow::before { background: var(--yellow); box-shadow: 0 0 12px rgba(251,191,36,0.7); }
.kpi-card.red::before { background: var(--red); box-shadow: 0 0 12px rgba(248,113,113,0.7); }
.kpi-icon { width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; background: rgba(148,163,184,0.1); }
.kpi-num { font-size: 26px; font-weight: 750; line-height: 1.1; }
.kpi-card.blue .kpi-num { color: var(--blue); }
.kpi-card.green .kpi-num { color: var(--green); }
.kpi-card.yellow .kpi-num { color: var(--yellow); }
.kpi-card.red .kpi-num { color: var(--red); }
.kpi-num .unit { font-size: 13px; font-weight: 600; margin-left: 2px; }
.kpi-label { color: var(--muted); font-size: 12px; margin-top: 2px; }
.kpi-sub { position: absolute; right: 14px; top: 14px; font-size: 11px; color: var(--muted); background: var(--panel2); padding: 2px 8px; border-radius: 999px; }

/* 图表 */
.chart-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.chart-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 14px 10px; box-shadow: var(--shadow-sm); }
.chart-card.wide { grid-column: span 2; }
.chart-card h4 { font-size: 13px; letter-spacing: 0.3px; margin-bottom: 6px; color: var(--muted); }
.chart-title-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.chart-title-row h4 { font-size: 13px; letter-spacing: 0.3px; color: var(--muted); margin: 0; }
.tab-group { display: flex; gap: 0; }
.tab-btn { padding: 4px 12px; font-size: 12px; border: 1px solid var(--border); background: var(--panel2); color: var(--muted); cursor: pointer; }
.tab-btn:first-child { border-radius: 6px 0 0 6px; }
.tab-btn:last-child { border-radius: 0 6px 6px 0; }
.tab-btn.active { background: var(--blue); color: #fff; border-color: var(--blue); }
.chart-container { display: flex; align-items: center; gap: 12px; }
.chart-box { width: 150px; height: 160px; flex-shrink: 0; }
.chart-legend { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.legend-item { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.red { background: var(--red); }
.dot.yellow { background: var(--yellow); }
.dot.green { background: var(--green); }
.dot.blue { background: var(--blue); }
.dot.gray { background: var(--gray); }

.full-width { grid-column: 1 / -1; }
.card { padding: 14px 16px; }
.meta-item { font-size: 12px; color: var(--muted); }

/* 风险条 */
.risk-bars { display: flex; flex-direction: column; gap: 14px; padding-top: 4px; }
.risk-item { display: flex; align-items: center; gap: 14px; }
.risk-label { width: 110px; font-size: 13px; color: var(--muted); flex-shrink: 0; }
.risk-track { flex: 1; height: 18px; background: var(--panel2); border-radius: 9px; overflow: hidden; }
.risk-fill { height: 100%; border-radius: 9px; transition: width 0.5s ease; }
.risk-fill.red { background: linear-gradient(90deg, #f87171, #dc2626); }
.risk-fill.yellow { background: linear-gradient(90deg, #fbbf24, #d97706); }
.risk-fill.green { background: linear-gradient(90deg, #34d399, #059669); }
.risk-val { width: 120px; font-size: 13px; color: var(--text); text-align: right; flex-shrink: 0; }

@media (max-width: 900px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .chart-row { grid-template-columns: 1fr; }
  .chart-card.wide { grid-column: 1 / -1; }
  .header-right { width: 100%; margin-left: 0; flex-wrap: wrap; }
}
</style>
