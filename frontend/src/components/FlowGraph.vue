<template>
  <div ref="el" style="width: 100%; height: 340px"></div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

// 图结构（docs/06 LangGraph 节点与边）
const NODES = [
  { id: 'intake', label: 'Intake\n受理' },
  { id: 'ocr', label: 'Evidence\n多模态凭证' },
  { id: 'fraud', label: 'Fraud\n欺诈风控' },
  { id: 'sentiment', label: 'Sentiment\n舆情分析' },
  { id: 'amount_decision', label: '金额决策' },
  { id: 'human_review', label: 'HumanReview\n人工审批' },
  { id: 'refund_execution', label: '退款执行' },
]
const EDGES = [
  ['intake', 'ocr'],
  ['ocr', 'fraud'],
  ['fraud', 'sentiment'],
  ['sentiment', 'amount_decision'],
  ['amount_decision', 'human_review'],
  ['amount_decision', 'refund_execution'],
  ['human_review', 'refund_execution'],
  ['human_review', 'end'],
  ['refund_execution', 'end'],
]

const props = defineProps({
  agents: { type: Array, default: () => [] }, // agent_execution_logs 轨迹
  currentStatus: { type: String, default: 'RUNNING' },
  currentNode: { type: String, default: '' },
  finalDecision: { type: String, default: 'PENDING' },
  refundStatus: { type: String, default: 'NONE' }, // ：流程图展示退款状态
})

const el = ref(null)
let chart = null
let resizeObserver = null

// 节点状态：done=绿 / suspended=黄(挂起等待) / active=蓝(当前执行) / idle=灰(未执行)
// ：退款执行节点按独立退款状态着色（SUCCESS 绿 / FAILED 红），
// 区分"业务决策 APPROVED"与"退款是否到账"两个口径。
function nodeState(id) {
  const done = props.agents.some((a) => a.agent_name === id && a.status === 'SUCCESS')
  if (id === 'refund_execution' && props.refundStatus === 'SUCCESS') return 'done'
  if (id === 'refund_execution' && props.refundStatus === 'FAILED') return 'failed'
  if (done) return 'done'
  if (id === 'end' && props.currentStatus === 'COMPLETED') return 'done'
  if (props.currentStatus === 'SUSPENDED' && id === 'human_review') return 'suspended'
  if (id === props.currentNode) return 'active'
  return 'idle'
}
const REFUND_TEXT = { NONE: '未发起', PENDING: '退款中', SUCCESS: '已退款', FAILED: '退款失败' }
const refundTooltip = `退款状态：${REFUND_TEXT[props.refundStatus] || props.refundStatus}`
const STATE_TEXT = { done: '已完成', suspended: '挂起等待', active: '执行中', idle: '未执行' }
const COLORS = { done: '#10b981', suspended: '#f59e0b', active: '#3b82f6', idle: '#cbd5e1', failed: '#ef4444' }

function render() {
  if (!chart) return
  // 浅色主题下文字颜色：深色底节点用白字，浅灰底用深字
  const labelColor = (st) => (st === 'idle' || st === 'suspended' ? '#334155' : '#ffffff')
  const graphNodes = [
    ...NODES.map((n) => {
      const st = nodeState(n.id)
      return {
        id: n.id,
        name: n.label,
        symbol: 'roundRect',
        symbolSize: [88, 42],
        itemStyle: {
          color: COLORS[st],
          borderColor: st === 'active' ? '#1e3a8a' : 'transparent',
          borderWidth: st === 'active' ? 2 : 0,
          shadowBlur: st === 'active' ? 14 : 0,
          shadowColor: 'rgba(59, 130, 246, 0.45)',
        },
        label: { color: labelColor(st), fontSize: 11, fontWeight: st === 'done' ? 600 : 500 },
        tooltip: { formatter: `${n.label.replace('\n', ' ')}<br/><b>${STATE_TEXT[st]}</b>${n.id === 'refund_execution' ? `<br/>${refundTooltip}` : ''}` },
      }
    }),
    {
      id: 'end',
      name: '终态',
      symbol: 'circle',
      symbolSize: 40,
      itemStyle: { color: nodeState('end') === 'done' ? '#10b981' : '#cbd5e1' },
      label: { color: '#334155', fontSize: 11 },
      tooltip: {
        formatter: `终态<br/><b>${nodeState('end') === 'done' ? '已完成' : '未到达'}</b>`,
      },
    },
  ]
  const graphEdges = EDGES.map(([s, t]) => {
    const done = nodeState(s) === 'done' && nodeState(t) === 'done'
    return {
      source: s, target: t,
      lineStyle: { color: done ? 'rgba(16, 185, 129, 0.55)' : '#b8c7e0', width: done ? 2 : 1.5 },
    }
  })
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    animationDurationUpdate: 300,
    series: [{
      type: 'graph',
      layout: 'circular',
      roam: true,
      data: graphNodes,
      edges: graphEdges,
      label: { show: true },
      emphasis: { focus: 'adjacency', lineStyle: { width: 2.5 } },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 7],
      lineStyle: { color: '#b8c7e0', width: 1.5, curveness: 0.08 },
      animationDuration: 500,
    }],
  })
}

onMounted(() => {
  chart = echarts.init(el.value)
  render()
  resizeObserver = new ResizeObserver(() => chart && chart.resize())
  resizeObserver.observe(el.value)
})

watch(() => [props.agents, props.currentStatus, props.currentNode, props.finalDecision, props.refundStatus], render)

onUnmounted(() => {
  resizeObserver && resizeObserver.disconnect()
  chart && chart.dispose()
})
</script>
