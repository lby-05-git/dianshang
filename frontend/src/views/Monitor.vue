<template>
  <div class="monitor-page">
    <!-- 顶栏 -->
    <div class="monitor-header">
      <div class="header-left">
        <h2>📊 实时监控大盘</h2>
        <span class="header-badge" :class="online ? 'badge-online' : 'badge-offline'">
          {{ online ? '● 在线' : '○ 离线' }}
        </span>
      </div>
      <div class="header-right">
        <span class="meta-item">🔄 {{ refreshInterval }}s 自动刷新</span>
        <span class="meta-item" v-if="lastUpdate">更新：{{ lastUpdate }}</span>
        <button class="btn gray" style="padding: 4px 12px; font-size: 12px" @click="refresh">🔄 刷新</button>
        <a class="back-link" href="#/">← 工作台</a>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="loading-box">
      <div class="spinner"></div>
      <div class="loading-text">加载监控数据…</div>
    </div>

    <template v-else>
      <!-- KPI 行 -->
      <div class="kpi-row">
        <div class="kpi-card blue" @click="curTrend='hourly'">
          <div class="kpi-icon">📋</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ data.total }}</div>
            <div class="kpi-label">累计工单</div>
          </div>
          <div class="kpi-sub">
            今日 <strong>+{{ data.today_count }}</strong>
            <span v-if="data.yesterday_count > 0" :class="trendCls(data.today_count, data.yesterday_count)">
              {{ trendArrow(data.today_count, data.yesterday_count) }}{{ trendPct(data.today_count, data.yesterday_count) }}
            </span>
          </div>
        </div>
        <div class="kpi-card green">
          <div class="kpi-icon">✅</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ data.status_dist.COMPLETED }}</div>
            <div class="kpi-label">已完成</div>
          </div>
          <div class="kpi-sub">占比 {{ pct(data.status_dist.COMPLETED, data.total) }}%</div>
        </div>
        <div class="kpi-card yellow">
          <div class="kpi-icon">⏸️</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ data.status_dist.SUSPENDED }}</div>
            <div class="kpi-label">挂起待审</div>
          </div>
          <div class="kpi-sub">占比 {{ pct(data.status_dist.SUSPENDED, data.total) }}%</div>
        </div>
        <div class="kpi-card red">
          <div class="kpi-icon">🔴</div>
          <div class="kpi-body">
            <div class="kpi-num">{{ data.decision_dist.SYSTEM_FAILURE }}</div>
            <div class="kpi-label">系统失败</div>
          </div>
          <div class="kpi-sub">需人工处理</div>
        </div>
      </div>

      <!-- 工单趋势图（近 24h / 近 7 天切换） -->
      <div class="card chart-card full-width">
        <div class="chart-title-row">
          <h4>📈 工单趋势</h4>
          <div class="tab-group">
            <button class="tab-btn" :class="{ active: curTrend === 'hourly' }" @click="curTrend='hourly'">近 24 小时</button>
            <button class="tab-btn" :class="{ active: curTrend === 'daily' }" @click="curTrend='daily'">近 7 天</button>
          </div>
        </div>
        <div ref="trendChart" class="chart-box" style="height: 200px; width: 100%"></div>
      </div>

      <!-- 图表行：决策分布 + 风险分布 + 决策模式 -->
      <div class="chart-row">
        <div class="chart-card">
          <h4>决策分布</h4>
          <div class="chart-container">
            <div ref="decisionChart" class="chart-box"></div>
            <div class="chart-legend">
              <div class="legend-item"><span class="dot green"></span>已批准 {{ data.decision_dist.APPROVED }}</div>
              <div class="legend-item"><span class="dot red"></span>已拒绝 {{ data.decision_dist.REJECTED }}</div>
              <div class="legend-item"><span class="dot gray"></span>待决策 {{ data.decision_dist.PENDING }}</div>
              <div class="legend-item"><span class="dot red" style="background:#7f1d1d"></span>系统失败 {{ data.decision_dist.SYSTEM_FAILURE }}</div>
            </div>
          </div>
        </div>
        <div class="chart-card">
          <h4>风险等级分布</h4>
          <div class="chart-container">
            <div ref="riskChart" class="chart-box"></div>
            <div class="chart-legend">
              <div class="legend-item"><span class="dot red"></span>高风险 (≥60) {{ data.risk_dist.HIGH }}</div>
              <div class="legend-item"><span class="dot yellow"></span>中风险 (21-59) {{ data.risk_dist.MEDIUM }}</div>
              <div class="legend-item"><span class="dot green"></span>低风险 (≤20) {{ data.risk_dist.LOW }}</div>
            </div>
          </div>
        </div>
        <div class="chart-card">
          <h4>决策模式</h4>
          <div class="chart-container">
            <div ref="modeChart" class="chart-box"></div>
            <div class="chart-legend">
              <div class="legend-item"><span class="dot blue"></span>自动秒退 {{ data.auto_vs_manual.auto }}</div>
              <div class="legend-item"><span class="dot yellow"></span>人工决策 {{ data.auto_vs_manual.manual }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Agent 双图：耗时 + 成功率 -->
      <div class="chart-row">
        <div class="chart-card chart-card-half">
          <h4>⏱️ Agent 平均耗时（毫秒）</h4>
          <div ref="agentChart" class="chart-box" style="height: 200px; width: 100%"></div>
        </div>
        <div class="chart-card chart-card-half">
          <h4>✅ Agent 成功率</h4>
          <div ref="successChart" class="chart-box" style="height: 200px; width: 100%"></div>
        </div>
      </div>

      <!-- 最近工单 -->
      <div class="card full-width">
        <div class="chart-title-row">
          <h4>📋 最近工单</h4>
          <span class="meta-item">共 {{ data.recent_cases.length }} 条</span>
        </div>
        <div v-if="!data.recent_cases.length" class="empty">暂无工单数据</div>
        <table v-else class="recent-table">
          <thead>
            <tr>
              <th>案件</th>
              <th>金额</th>
              <th>状态</th>
              <th>决策</th>
              <th>当前节点</th>
              <th>欺诈分</th>
              <th>舆情</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in data.recent_cases" :key="c.case_id">
              <td class="mono">{{ c.case_id }}</td>
              <td>¥{{ c.amount.toFixed(2) }}</td>
              <td><span class="tag" :class="statusTag(c.status)">{{ statusText(c.status) }}</span></td>
              <td><span class="tag" :class="decisionTag(c.decision)">{{ decisionText(c.decision) }}</span></td>
              <td class="muted">{{ nodeText(c.node) }}</td>
              <td>{{ c.fraud_score ?? '-' }}</td>
              <td>{{ c.sentiment ?? '-' }}</td>
              <td class="muted">{{ c.created_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, nextTick, watch } from 'vue'
import { default as axios } from 'axios'
import * as echarts from 'echarts'

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
const lastUpdate = ref('')
const refreshTimer = ref(null)
const refreshInterval = ref(5)
const curTrend = ref('hourly')

const trendChart = ref(null)
const decisionChart = ref(null)
const riskChart = ref(null)
const modeChart = ref(null)
const agentChart = ref(null)
const successChart = ref(null)

const pct = (n, total) => total ? ((n / total) * 100).toFixed(1) : '0.0'
const trendCls = (a, b) => a > b ? 'trend-up' : a < b ? 'trend-down' : 'trend-flat'
const trendArrow = (a, b) => a > b ? '↑' : a < b ? '↓' : '→'
const trendPct = (a, b) => b > 0 ? ` ${Math.abs(((a - b) / b) * 100).toFixed(0)}%` : ''

const statusTag = (s) => ({ RUNNING: 'blue', SUSPENDED: 'yellow', COMPLETED: 'green' }[s] || 'gray')
const statusText = (s) => ({ RUNNING: '运行中', SUSPENDED: '挂起', COMPLETED: '已完成' }[s] || s)
const decisionTag = (d) => ({ PENDING: 'gray', APPROVED: 'green', REJECTED: 'red', SYSTEM_FAILURE: 'red' }[d] || 'gray')
const decisionText = (d) => ({ PENDING: '待决策', APPROVED: '已批准', REJECTED: '已拒绝', SYSTEM_FAILURE: '系统失败' }[d] || d)
const nodeText = (n) => ({ human_review: '人工审批', amount_decision: '金额决策', refund_execution: '退款执行', finalize: '已完成', intake: '受理中', ocr: 'OCR识别中', fraud: '风控中', sentiment: '舆情分析中' }[n] || n || '-')

let charts = []

function disposeCharts() {
  charts.forEach(c => { try { c.dispose() } catch {} })
  charts = []
}

function renderCharts() {
  disposeCharts()
  const ec = echarts

  // 趋势图
  const tChart = ec.init(trendChart.value)
  const trendData = curTrend.value === 'hourly' ? data.value.trend.hourly : data.value.trend.daily
  tChart.setOption({
    tooltip: { trigger: 'axis', formatter: (params) => `${params[0].name}<br/>工单数: ${params[0].value}` },
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

  // 风险分布
  const rChart = ec.init(riskChart.value)
  rChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['45%', '70%'], avoidLabelOverlap: true, label: { show: false },
      data: [
        { value: data.value.risk_dist.HIGH, name: '高风险', itemStyle: { color: '#dc2626' } },
        { value: data.value.risk_dist.MEDIUM, name: '中风险', itemStyle: { color: '#b45309' } },
        { value: data.value.risk_dist.LOW, name: '低风险', itemStyle: { color: '#059669' } },
      ],
    }],
  })
  charts.push(rChart)

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

  // Agent 耗时
  const aChart = ec.init(agentChart.value)
  const agentNames = { intake: '受理', ocr: 'OCR识别', fraud: '欺诈风控', sentiment: '舆情分析', amount_decision: '金额决策', refund_execution: '退款执行' }
  const agents = Object.keys(agentNames)
  aChart.setOption({
    tooltip: { trigger: 'axis', formatter: (params) => `${params[0].name}<br/>平均耗时: ${params[0].value}ms` },
    grid: { left: 50, right: 20, top: 20, bottom: 35 },
    xAxis: { type: 'category', data: agents.map(a => agentNames[a] || a), axisLabel: { rotate: 15, fontSize: 11 } },
    yAxis: { type: 'value', name: 'ms', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar', barWidth: 24,
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
    tooltip: { trigger: 'axis', formatter: (params) => `${params[0].name}<br/>成功率: ${params[0].value}%` },
    grid: { left: 50, right: 20, top: 20, bottom: 35 },
    xAxis: { type: 'category', data: agents.map(a => agentNames[a] || a), axisLabel: { rotate: 15, fontSize: 11 } },
    yAxis: { type: 'value', min: 90, max: 100, name: '%', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar', barWidth: 24,
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
}

async function refresh() {
  try {
    const res = await api.get('/dashboard/monitor')
    data.value = res.data
    refreshInterval.value = res.data.refresh_interval || 5
    online.value = true
    const now = new Date()
    lastUpdate.value = now.toLocaleTimeString()
    await nextTick()
    renderCharts()
  } catch (e) {
    online.value = false
    console.error('监控数据加载失败', e)
  } finally {
    loading.value = false
  }
}

// 趋势切换时重新渲染图表
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
.monitor-page { display: flex; flex-direction: column; gap: 14px; }

/* 顶栏 */
.monitor-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 12px 18px; box-shadow: var(--shadow-sm);
}
.header-left { display: flex; align-items: center; gap: 10px; }
.monitor-header h2 { font-size: 17px; letter-spacing: 0.5px; margin: 0; }
.header-badge { font-size: 11px; padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge-online { background: rgba(5, 150, 105, 0.12); color: var(--green); border: 1px solid rgba(5, 150, 105, 0.25); }
.badge-offline { background: rgba(220, 38, 38, 0.12); color: var(--red); border: 1px solid rgba(220, 38, 38, 0.25); }
.header-right { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-left: auto; align-items: center; }
.back-link { font-size: 12px; color: var(--blue); text-decoration: none; padding: 4px 10px; border: 1px solid rgba(96,165,250,0.3); border-radius: 6px; }
.back-link:hover { background: rgba(96,165,250,0.1); }

/* 加载 */
.loading-box { text-align: center; padding: 80px 16px; }
.loading-text { color: var(--muted); margin-top: 12px; }
.spinner { width: 32px; height: 32px; margin: 0 auto; border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* KPI */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; cursor: pointer; }
.kpi-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; display: flex; align-items: center; gap: 14px; position: relative; overflow: hidden; box-shadow: var(--shadow-sm); transition: transform 0.15s, box-shadow 0.15s; }
.kpi-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); }
.kpi-card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }
.kpi-card.blue::before { background: var(--blue); box-shadow: 0 0 12px rgba(96,165,250,0.7); }
.kpi-card.green::before { background: var(--green); box-shadow: 0 0 12px rgba(52,211,153,0.7); }
.kpi-card.yellow::before { background: var(--yellow); box-shadow: 0 0 12px rgba(251,191,36,0.7); }
.kpi-card.red::before { background: var(--red); box-shadow: 0 0 12px rgba(248,113,113,0.7); }
.kpi-icon { width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; background: rgba(148,163,184,0.1); }
.kpi-body { min-width: 0; }
.kpi-num { font-size: 26px; font-weight: 750; line-height: 1.1; }
.kpi-card.blue .kpi-num { color: var(--blue); }
.kpi-card.green .kpi-num { color: var(--green); }
.kpi-card.yellow .kpi-num { color: var(--yellow); }
.kpi-card.red .kpi-num { color: var(--red); }
.kpi-label { color: var(--muted); font-size: 12px; margin-top: 2px; }
.kpi-sub { position: absolute; right: 14px; top: 14px; font-size: 11px; color: var(--muted); background: var(--panel2); padding: 2px 8px; border-radius: 999px; display: flex; gap: 4px; align-items: center; }
.trend-up { color: var(--green); font-weight: 600; }
.trend-down { color: var(--red); font-weight: 600; }
.trend-flat { color: var(--muted); }

/* 图表标题行 */
.chart-title-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.chart-title-row h4 { font-size: 13px; letter-spacing: 0.3px; color: var(--muted); margin: 0; }
.tab-group { display: flex; gap: 0; }
.tab-btn { padding: 4px 12px; font-size: 12px; border: 1px solid var(--border); background: var(--panel2); color: var(--muted); cursor: pointer; }
.tab-btn:first-child { border-radius: 6px 0 0 6px; }
.tab-btn:last-child { border-radius: 0 6px 6px 0; }
.tab-btn.active { background: var(--blue); color: #fff; border-color: var(--blue); }

/* 图表行 */
.chart-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.chart-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 14px 10px; box-shadow: var(--shadow-sm); transition: box-shadow 0.2s; }
.chart-card:hover { box-shadow: var(--shadow-md); }
.chart-card h4 { font-size: 13px; letter-spacing: 0.3px; margin-bottom: 6px; color: var(--muted); }
.chart-card.full-width { grid-column: 1 / -1; }
.chart-card-half { grid-column: span 1; }
.chart-card.half-width { grid-column: span 1; }
.chart-container { display: flex; align-items: center; gap: 12px; }
.chart-box { width: 160px; height: 160px; flex-shrink: 0; }
.chart-legend { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.legend-item { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.red { background: var(--red); }
.dot.yellow { background: var(--yellow); }
.dot.green { background: var(--green); }
.dot.blue { background: var(--blue); }
.dot.gray { background: var(--gray); }

/* 全宽 */
.full-width { grid-column: 1 / -1; }

/* 表格 */
.recent-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.recent-table th { text-align: left; padding: 8px 10px; color: var(--muted); font-weight: 550; font-size: 12px; letter-spacing: 0.4px; border-bottom: 1px solid var(--border); background: var(--panel2); }
.recent-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
.recent-table tr:last-child td { border-bottom: none; }
.recent-table tr:hover { background: rgba(37,99,235,0.045); }
.mono { font-family: Consolas, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.empty { text-align: center; padding: 24px; color: var(--muted); }

/* 响应式 */
@media (max-width: 900px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .chart-row { grid-template-columns: 1fr; }
  .chart-card-half { grid-column: 1 / -1; }
  .header-right { width: 100%; margin-left: 0; flex-wrap: wrap; }
}
</style>
