<template>
  <div class="eval-page">
    <!-- 顶栏 -->
    <div class="eval-header">
      <div class="header-left">
        <h2>🚀 评测中心</h2>
        <span class="header-badge" :class="online ? 'badge-online' : 'badge-offline'">
          {{ online ? '● 在线' : '○ 离线' }}
        </span>
      </div>
      <div class="header-right">
        <span class="meta-item">数据不可变 · 改版靠复制 · 评测永不调用真实退款</span>
        <button class="btn one-click-btn" :disabled="oneClickBusy || oneClickActive" @click="startOneClickEvaluation">
          {{ oneClickButtonText }}
        </button>
        <button class="btn gray" style="padding: 4px 12px; font-size: 12px" @click="refreshAll">🔄 刷新</button>
        <a class="back-link" href="#/">← 工作台</a>
      </div>
    </div>

    <!-- 标签页 -->
    <div class="tab-bar">
      <button v-for="t in tabs" :key="t.key" class="tab" :class="{ active: curTab === t.key }"
              @click="switchTab(t.key)">
        {{ t.icon }} {{ t.label }}
      </button>
    </div>

    <section class="scorecard-section">
      <div class="scorecard-heading">
        <div>
          <h3>三维评测评分</h3>
          <p>最近完成 Run：<span class="mono">{{ scorecard.run_id || '暂无' }}</span> · 未测维度不生成虚假分数</p>
        </div>
        <span class="tag" :class="scorecard.status === 'READY' ? 'green' : 'gray'">
          {{ scorecard.status === 'READY' ? '已实测' : 'PENDING' }}
        </span>
      </div>
      <div class="score-grid">
        <article v-for="dimension in scoreDimensions" :key="dimension.key" class="score-item">
          <div class="score-topline">
            <span>{{ dimension.label }}</span>
            <strong :class="scoreClass(dimension.score)">{{ scoreText(dimension) }}</strong>
          </div>
          <div class="score-track" :class="{ pending: dimension.score == null }">
            <div class="score-fill" :style="{ width: dimension.score == null ? '0%' : dimension.score + '%' }"></div>
          </div>
          <p>{{ dimension.explanation }}</p>
          <small>{{ dimension.score == null ? '等待完成评测' : `已测 ${dimension.measured} 项 · 通过 ${dimension.passed} 项` }}</small>
        </article>
      </div>
    </section>

    <!-- 周期意图对照评测 -->
    <section v-if="curTab === 'intent-eval'" class="section intent-eval-section">
      <div class="section-head">
        <div>
          <h3>🧭 意图周期评测与商城链路</h3>
          <p class="muted small intent-subtitle">同一批标签样本对煬“纯 LLM”与“规则 + LLM 双层路由”，只读回放，不执行退款。</p>
        </div>
        <div class="head-actions">
          <button class="btn gray" @click="switchTab('schedules')">查看周期调度</button>
          <button class="btn" :disabled="intentEvalBusy || !!intentActiveRun" @click="startIntentEvaluation">
            {{ intentEvalButtonText }}
          </button>
        </div>
      </div>

      <div class="intent-meta-bar">
        <span><b>数据集</b> {{ intentDataset.version || '—' }}</span>
        <span><b>样本</b> 意图 {{ intentDataset.sample_count || 120 }} 条 + 商城退换货 {{ intentDataset.shopping_flow_sample_count || 100 }} 条</span>
        <span><b>周期</b>
          <template v-if="intentSchedule">
            {{ cadenceText(intentSchedule.cadence) }} · {{ intentSchedule.enabled ? '已启用' : '已停用' }}
            · 下次 {{ fmtTime(intentSchedule.next_due_at) }}
          </template>
          <template v-else>启动 Worker 后自动创建每日调度</template>
        </span>
        <span><b>最近 Run</b> <span class="mono">{{ intentLatest?.run_id || '—' }}</span></span>
      </div>

      <div v-if="intentEvalLoading" class="empty"><div class="spinner"></div></div>
      <template v-else-if="intentLatest && intentLatest.summary">
        <div class="intent-kpi-grid">
          <article class="intent-kpi">
            <div class="intent-kpi-title">召回率 <span class="target-chip">目标 ≥90%</span></div>
            <strong :class="metricClass(intentSummary.macro_recall, 0.9, true)">{{ pctText(intentSummary.macro_recall) }}</strong>
            <div class="metric-compare">纯 LLM {{ pctText(intentSummary.pure_llm?.macro_recall) }} <span>→</span> 双层 {{ pctText(intentSummary.hybrid?.macro_recall) }}</div>
            <small>宏平均召回；退款意图 {{ pctText(intentSummary.refund_recall) }}</small>
          </article>
          <article class="intent-kpi">
            <div class="intent-kpi-title">幻觉率 <span class="target-chip">目标 ≤2%</span></div>
            <strong :class="metricClass(intentSummary.hallucination_rate, 0.02, false)">{{ pctText(intentSummary.hallucination_rate) }}</strong>
            <div class="metric-compare">纯 LLM {{ pctText(intentSummary.pure_llm?.hallucination_rate) }} <span>→</span> 双层 {{ pctText(intentSummary.hybrid?.hallucination_rate) }}</div>
            <small>预测意图与标注意图不一致的比例</small>
          </article>
          <article class="intent-kpi">
            <div class="intent-kpi-title">TTFT <span class="target-chip">{{ ttftMeasurementText }}</span></div>
            <strong>{{ msText(intentPerformance.ttft_ms?.hybrid?.avg) }}</strong>
            <div class="metric-compare">纯 LLM {{ msText(intentPerformance.ttft_ms?.pure_llm?.avg) }} <span>→</span> 双层 {{ msText(intentPerformance.ttft_ms?.hybrid?.avg) }}</div>
            <small>双层 P95 {{ msText(intentPerformance.ttft_ms?.hybrid?.p95) }} · 降低 {{ pctText(intentPerformance.ttft_ms?.reduction) }}</small>
          </article>
          <article class="intent-kpi">
            <div class="intent-kpi-title">Token 降幅 <span class="target-chip">目标 ≥40%</span></div>
            <strong :class="metricClass(intentCost.token_gain, 0.4, true)">{{ pctText(intentCost.token_gain) }}</strong>
            <div class="metric-compare">纯 LLM {{ formatNumber(intentCost.pure_llm_tokens) }} <span>→</span> 双层 {{ formatNumber(intentCost.hybrid_tokens) }}</div>
            <small>减少 {{ formatNumber(intentCost.saved_tokens) }} Token · {{ intentCost.measured ? 'Provider usage 实测' : '同口径估算' }}</small>
          </article>
        </div>

        <div class="intent-compare-grid">
          <div class="intent-panel">
            <div class="panel-title"><span>纯 LLM 基准</span><span class="tag gray">{{ intentModeText }}</span></div>
            <dl class="compare-list">
              <div><dt>所有样本进入 LLM</dt><dd>{{ intentPerformance.llm_calls?.pure_llm ?? '—' }} 次</dd></div>
              <div><dt>正确 / 总数</dt><dd>{{ intentSummary.pure_llm?.correct ?? '—' }} / {{ intentSummary.total }}</dd></div>
              <div><dt>平均 TTFT</dt><dd>{{ msText(intentPerformance.ttft_ms?.pure_llm?.avg) }}</dd></div>
              <div><dt>Token</dt><dd>{{ formatNumber(intentCost.pure_llm_tokens) }}</dd></div>
            </dl>
          </div>
          <div class="intent-panel highlighted">
            <div class="panel-title"><span>双层意图路由</span><span class="tag green">生产路径</span></div>
            <dl class="compare-list">
              <div><dt>需调用 LLM</dt><dd>{{ intentPerformance.llm_calls?.hybrid ?? '—' }} 次</dd></div>
              <div><dt>正确 / 总数</dt><dd>{{ intentSummary.hybrid?.correct ?? '—' }} / {{ intentSummary.total }}</dd></div>
              <div><dt>平均 TTFT</dt><dd>{{ msText(intentPerformance.ttft_ms?.hybrid?.avg) }}</dd></div>
              <div><dt>Token</dt><dd>{{ formatNumber(intentCost.hybrid_tokens) }}</dd></div>
            </dl>
          </div>
        </div>

        <div class="truth-note">
          <b>口径说明：</b>{{ intentCost.note || '—' }}
          离线模式中的“纯 LLM”为确定性语义代理，用于无费用持续回归；开启真模型配置后 TTFT 才是云/Ollama 墙钟实测。
        </div>

        <div class="intent-panel shopping-flow-panel">
          <div class="panel-title">
            <span>🛒 商城下单 → 退货/换货 → 凭证 OCR 链路</span>
            <span class="tag" :class="shoppingFlow.measured ? 'green' : 'gray'">
              {{ shoppingFlow.measured ? '真实 OCR 实测' : shoppingFlow.status === 'UNAVAILABLE' ? 'OCR 暂不可用' : '待评测' }}
            </span>
          </div>
          <div class="shopping-flow-grid">
            <div><strong>{{ shoppingFlow.unique_users ?? shoppingFlow.sample_count ?? '—' }}</strong><span>模拟商城用户</span></div>
            <div><strong>{{ pctText(shoppingFlow.completion_rate) }}</strong><span>完整链路通过率</span></div>
            <div><strong>{{ pctText(shoppingFlow.order_accuracy) }}</strong><span>OCR 订单号准确率</span></div>
            <div><strong>{{ pctText(shoppingFlow.product_accuracy) }}</strong><span>OCR 商品准确率</span></div>
            <div><strong>{{ pctText(shoppingFlow.amount_accuracy) }}</strong><span>OCR 金额准确率</span></div>
            <div><strong>{{ msText(shoppingFlow.ocr_latency_ms?.p95) }}</strong><span>OCR P95</span></div>
          </div>
          <div class="shopping-flow-facts">
            <span>退货 {{ shoppingFlow.aftersale_distribution?.return ?? '—' }} 条</span>
            <span>换货 {{ shoppingFlow.aftersale_distribution?.exchange ?? '—' }} 条</span>
            <span>业务写入 {{ shoppingFlow.isolation?.business_writes ?? '—' }}</span>
            <span>真实退款 {{ shoppingFlow.isolation?.real_refunds ?? '—' }}</span>
            <span>隔离模式 {{ shoppingFlow.isolation?.mode || '—' }}</span>
          </div>
          <p v-if="shoppingFlow.error" class="shopping-flow-error">{{ shoppingFlow.error }}</p>
          <p v-else-if="shoppingFlow.sample_count" class="muted small">每条回放都会在临时目录生成与商城一致的电子订单凭证，再由真实 OCR 提取订单号、商品与金额；结束后临时图片自动清理。</p>
          <p v-else class="muted small">当前展示的是旧版 Run；请点击“立即执行”生成 100 条商城退换货 OCR 链路实测结果。</p>
        </div>

        <div class="intent-panel intent-breakdown">
          <div class="panel-title"><span>6 类意图召回明细</span><span class="muted small">每类 20 条，覆盖率 {{ pctText(intentSummary.coverage) }}</span></div>
          <table class="table">
            <thead><tr><th>意图</th><th>样本数</th><th>双层命中</th><th>双层召回率</th><th>纯 LLM 召回率</th></tr></thead>
            <tbody>
              <tr v-for="row in intentBreakdown" :key="row.intent">
                <td><span class="tag blue">{{ intentName(row.intent) }}</span></td>
                <td>{{ row.samples }}</td><td>{{ row.true_positive }}</td>
                <td><span class="tag" :class="row.recall >= 0.9 ? 'green' : 'red'">{{ pctText(row.recall) }}</span></td>
                <td>{{ pctText(row.pureRecall) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="intent-empty">
        <div class="empty-icon">🧭</div>
        <h4>尚无意图评测结果</h4>
        <p>点击“立即执行”，或启动 Worker 等待每日调度自动执行。</p>
      </div>

      <div class="section-head history-head"><h3>运行历史</h3></div>
      <table class="table">
        <thead><tr><th>Run</th><th>触发方式</th><th>样本</th><th>召回率</th><th>幻觉率</th><th>Token 节省</th><th>状态</th><th>完成时间</th></tr></thead>
        <tbody>
          <tr v-if="!intentEvalRuns.length"><td colspan="8" class="muted center">暂无历史</td></tr>
          <tr v-for="r in intentEvalRuns" :key="r.run_id">
            <td class="mono small">{{ short(r.run_id, 24) }}</td>
            <td>{{ r.trigger_source === 'schedule' ? '周期调度' : '手动' }}</td>
            <td>{{ r.summary?.total_cases ?? r.summary?.total ?? '—' }}</td>
            <td>{{ pctText(r.summary?.macro_recall) }}</td>
            <td>{{ pctText(r.summary?.hallucination_rate) }}</td>
            <td>{{ pctText(r.cost?.token_gain) }}</td>
            <td><span class="tag" :class="statusTag(r.status)">{{ r.status }}</span></td>
            <td class="small muted">{{ fmtTime(r.finished_at || r.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 数据集 -->
    <section v-if="curTab === 'datasets'" class="section">
      <div class="section-head">
        <h3>🗂️ 数据集（不可变版本）</h3>
        <div v-if="canManageEvaluation" class="head-actions">
          <button class="btn" @click="openCreateSuite">+ 从 Golden 建版</button>
          <button class="btn gray" @click="openImport">📄 导入 Excel/CSV</button>
        </div>
      </div>
      <div v-if="suitesLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>名称</th><th>版本</th><th>用例数</th><th>校验和</th><th>状态</th><th>变更说明</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!suites.length"><td colspan="8" class="muted center">暂无数据集</td></tr>
          <tr v-for="s in suites" :key="s.id">
            <td><span class="tag blue">{{ s.name }}</span></td>
            <td class="mono">{{ s.version }}</td>
            <td>{{ s.case_count }}</td>
            <td class="mono small muted">{{ short(s.checksum, 12) }}</td>
            <td><span class="tag" :class="s.status === 'ACTIVE' ? 'green' : 'gray'">{{ s.status }}</span></td>
            <td class="muted small">{{ s.changelog || '—' }}</td>
            <td class="small muted">{{ fmtTime(s.created_at) }}</td>
            <td class="actions">
              <button class="btn gray small-btn" @click="viewSuite(s)">用例</button>
              <button v-if="canManageEvaluation" class="btn gray small-btn" @click="openCopy(s)">复制</button>
              <button class="btn red small-btn" v-if="canAdmin" @click="removeSuite(s)">删</button>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 创建/复制数据集弹窗（不可变版本） -->
      <div v-if="createSuiteOpen" class="modal-mask" @click.self="createSuiteOpen = false">
        <div class="card modal">
          <h3>{{ copyTarget ? '📋 复制建新版（基线只读）' : '🗂️ 从当前 Golden 创建数据集版本' }}</h3>
          <label>名称</label>
          <input v-model="suiteName" placeholder="数据集名称" />
          <label>变更说明</label>
          <input v-model="suiteChangelog" placeholder="为何建此版本（changelog）" />
          <div v-if="suiteErr" class="error-text">{{ suiteErr }}</div>
          <div class="modal-actions">
            <button class="btn" :disabled="suiteSaving" @click="submitSuite">{{ suiteSaving ? '保存中…' : copyTarget ? '复制' : '创建' }}</button>
            <button class="btn gray" @click="createSuiteOpen = false">取消</button>
          </div>
        </div>
      </div>

      <!-- 数据集用例弹窗 -->
      <div v-if="viewingSuite" class="modal-mask" @click.self="viewingSuite = null">
        <div class="card modal wide">
          <div class="modal-head">
            <h3>🔎 数据集用例：{{ viewingSuite.name }} (v{{ viewingSuite.version }})</h3>
            <button class="btn gray small-btn" @click="viewingSuite = null">关闭</button>
          </div>
          <div v-if="casesLoading" class="empty"><div class="spinner"></div></div>
          <table v-else class="table small">
            <thead>
              <tr><th>Case</th><th>类型</th><th>订单号</th><th>金额</th><th>预期决策</th><th>预期退款</th><th>原因</th></tr>
            </thead>
            <tbody>
              <tr v-for="c in suiteCases" :key="c.case_id">
                <td class="mono">{{ c.case_id }}</td>
                <td><span class="tag gray">{{ c.case_type }}</span></td>
                <td class="mono small">{{ c.inputs.order_id }}</td>
                <td>{{ c.inputs.requested_amount }}</td>
                <td><span class="tag" :class="c.expected.final_decision === 'APPROVED' ? 'green' : 'red'">{{ c.expected.final_decision }}</span></td>
                <td><span class="tag">{{ c.expected.refund_status }}</span></td>
                <td class="muted small reason">{{ c.inputs.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 任务 Runs -->
    <section v-if="curTab === 'runs'" class="section">
      <div class="section-head">
        <h3>⚙️ 评测任务 Runs（异步 Worker）</h3>
        <div v-if="canManageEvaluation" class="head-actions">
          <button class="btn" @click="openCreateRun">+ 创建 Run</button>
        </div>
      </div>
      <div v-if="runsLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>Run</th><th>任务</th><th>数据集</th><th>进度</th><th>通过率</th><th>状态</th><th>成本实测</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!runs.length"><td colspan="9" class="muted center">暂无评测任务</td></tr>
          <tr v-for="r in runs" :key="r.run_id">
            <td class="mono small">{{ short(r.run_id, 24) }}</td>
            <td class="small">{{ r.name }}</td>
            <td class="small muted">{{ short(r.suite_id, 8) }} v{{ r.suite_version }}</td>
            <td class="progress-cell">
              <div class="progress-track"><div class="progress-fill" :style="{ width: runPct(r) + '%' }"></div></div>
              <span class="small muted">{{ runPct(r) }}%</span>
            </td>
            <td><span class="tag" :class="rateTag(r)">{{ rateText(r) }}</span></td>
            <td><span class="tag" :class="statusTag(r.status)">{{ r.status }}</span></td>
            <td><span class="tag" :class="r.cost && r.cost.measured ? 'green' : 'gray'">{{ r.cost && r.cost.measured ? '实测' : '未测' }}</span></td>
            <td class="small muted">{{ fmtTime(r.created_at) }}</td>
            <td class="actions">
              <button class="btn gray small-btn" @click="viewRun(r)">详情</button>
              <button class="btn red small-btn" v-if="canCancel(r)" @click="cancelRun(r)">取消</button>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 创建 Run 弹窗 -->
      <div v-if="createRunOpen" class="modal-mask" @click.self="createRunOpen = false">
        <div class="card modal">
          <h3>⚙️ 创建评测 Run</h3>
          <label>名称</label>
          <input v-model="runForm.name" placeholder="例如：V2.0 全量回归" />
          <label>数据集</label>
          <select v-model="runForm.suite_id">
            <option v-for="s in suites" :key="s.id" :value="s.id">
              {{ s.name }} (v{{ s.version }} · {{ s.case_count }} 条)
            </option>
          </select>
          <label>执行模式</label>
          <select v-model="runForm.mode">
            <option value="in_process">进程内（GoldenFollower）</option>
            <option value="http">黑盒 HTTP Target（Mock）</option>
          </select>
          <label>限制用例数（0 = 全量）</label>
          <input v-model.number="runForm.limit" type="number" min="0" max="300" />
          <label>Prompt 版本（可选，须已人工批准）</label>
          <select v-model="runForm.prompt_id">
            <option value="">（不绑定）</option>
            <option v-for="p in approvedPrompts" :key="p.id" :value="p.id">
              {{ p.name }} ({{ p.agent_name }} · {{ p.version }})
            </option>
          </select>
          <div v-if="runErr" class="error-text">{{ runErr }}</div>
          <div class="modal-actions">
            <button class="btn" :disabled="runSaving" @click="submitRun">{{ runSaving ? '提交中…' : '创建并执行' }}</button>
            <button class="btn gray" @click="createRunOpen = false">取消</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 报告 -->
    <section v-if="curTab === 'reports'" class="section">
      <div class="section-head">
        <h3>📊 报告与对比</h3>
        <div class="head-actions">
          <select v-model="cmpA" class="cmp-select"><option value="">Run A…</option>
            <option v-for="r in reports" :key="r.run_id" :value="r.run_id">{{ r.run_id }}</option></select>
          <span class="muted small">vs</span>
          <select v-model="cmpB" class="cmp-select"><option value="">Run B…</option>
            <option v-for="r in reports" :key="r.run_id" :value="r.run_id">{{ r.run_id }}</option></select>
          <button class="btn" :disabled="!cmpA || !cmpB" @click="doCompare">对比</button>
        </div>
      </div>
      <div v-if="reportsLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>Run</th><th>数据集</th><th>总数</th><th>通过</th><th>失败</th><th>降级</th><th>通过率</th><th>成本实测</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!reports.length"><td colspan="10" class="muted center">暂无报告</td></tr>
          <tr v-for="r in reports" :key="r.run_id">
            <td class="mono small">{{ short(r.run_id, 24) }}</td>
            <td class="small muted">v{{ r.dataset_version }}</td>
            <td>{{ r.total ?? '—' }}</td>
            <td>{{ r.passed ?? '—' }}</td>
            <td>{{ r.failed ?? '—' }}</td>
            <td>{{ r.degraded ?? '—' }}</td>
            <td><span class="tag" :class="r.pass_rate != null && r.pass_rate >= 1 ? 'green' : 'yellow'">{{ pctText(r.pass_rate) }}</span></td>
            <td><span class="tag" :class="r.cost_measured ? 'green' : 'gray'">{{ r.cost_measured ? '实测' : '未测' }}</span></td>
            <td><span class="tag" :class="statusTag(r.status)">{{ r.status }}</span></td>
            <td class="actions">
              <button class="btn gray small-btn" @click="viewReport(r)">报告</button>
              <button v-if="canManageEvaluation" class="btn gray small-btn" @click="exportReport(r)">导出</button>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 报告详情弹窗 -->
      <div v-if="reportDoc" class="modal-mask" @click.self="reportDoc = null">
        <div class="card modal wide">
          <div class="modal-head">
            <h3>📄 报告：{{ short(reportDoc.run_id, 32) }}</h3>
            <button class="btn gray small-btn" @click="reportDoc = null">关闭</button>
          </div>
          <div class="report-kpis">
            <div class="kpi-mini"><div class="kpi-mini-num green">{{ reportDoc.summary.passed }}</div><div class="kpi-mini-label">通过</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num red">{{ reportDoc.summary.failed }}</div><div class="kpi-mini-label">失败</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num yellow">{{ reportDoc.summary.degraded }}</div><div class="kpi-mini-label">降级</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ pctText(reportDoc.summary.pass_rate) }}</div><div class="kpi-mini-label">通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ tokenText(reportDoc) }}</div><div class="kpi-mini-label">输入 tokens</div></div>
          </div>
          <table class="table small" style="max-height: 320px; overflow: auto">
            <thead><tr><th>Case</th><th>类型</th><th>判定</th><th>降级</th><th>退款</th><th>核验</th><th>耗时</th><th>证据</th></tr></thead>
            <tbody>
              <tr v-for="c in reportDoc.cases" :key="c.case_id">
                <td class="mono">{{ c.case_id }}</td>
                <td><span class="tag gray">{{ c.case_type }}</span></td>
                <td><span class="tag" :class="verdictTag(c.judge.verdict)">{{ c.judge.verdict }}</span></td>
                <td>{{ c.degraded ? '⚠️' : '—' }}</td>
                <td>
                  <template v-if="biz(c).refund_status">
                    <span class="tag" :class="rftag(biz(c).refund_status)">{{ rftext(biz(c).refund_status) }}</span>
                  </template>
                  <span v-else class="muted">—</span>
                </td>
                <td class="small">
                  <template v-if="biz(c).order_verification">
                    <span class="tag" :class="vestag(biz(c).order_verification)">{{ vetext(biz(c).order_verification) }}</span>
                    <span class="muted">/</span>
                    <span class="tag" :class="vestag(biz(c).product_consistency)">{{ vetext(biz(c).product_consistency) }}</span>
                    <span class="muted">/</span>
                    <span class="tag" :class="vestag(biz(c).price_consistency)">{{ vetext(biz(c).price_consistency) }}</span>
                  </template>
                  <span v-else class="muted">—</span>
                </td>
                <td class="small muted">{{ c.execution.latency_ms }}ms</td>
                <td class="muted small reason">{{ evidShort(c) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 对比结果弹窗 -->
      <div v-if="compareDoc" class="modal-mask" @click.self="compareDoc = null">
        <div class="card modal wide">
          <div class="modal-head">
            <h3>⚖️ 对比结果：{{ compareDoc.verdict }}</h3>
            <button class="btn gray small-btn" @click="compareDoc = null">关闭</button>
          </div>
          <div class="report-kpis">
            <div class="kpi-mini"><div class="kpi-mini-num">{{ pctText(compareDoc.run_a.pass_rate) }}</div><div class="kpi-mini-label">A 通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ pctText(compareDoc.run_b.pass_rate) }}</div><div class="kpi-mini-label">B 通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num" :class="deltaCls">{{ sign(compareDoc.deltas.pass_rate) }}</div><div class="kpi-mini-label">Δ 通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ compareDoc.deltas.common_cases }}</div><div class="kpi-mini-label">共同用例</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num red">{{ compareDoc.deltas.regressed_count }}</div><div class="kpi-mini-label">回归</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num green">{{ compareDoc.deltas.improved_count }}</div><div class="kpi-mini-label">改善</div></div>
          </div>
          <p v-if="compareDoc.regressed.length" class="error-text">回归用例：{{ compareDoc.regressed.join(', ') }}</p>
          <p v-if="compareDoc.improved.length" class="muted small">改善用例：{{ compareDoc.improved.join(', ') }}</p>
        </div>
      </div>
    </section>

    <!-- Prompt -->
    <section v-if="curTab === 'prompts'" class="section">
      <div class="section-head">
        <h3>⚡ Prompt 版本（人工批准门）</h3>
        <div v-if="canManageEvaluation" class="head-actions">
          <button class="btn" @click="openCreatePrompt">+ 新建 Prompt</button>
        </div>
      </div>
      <div v-if="promptsLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>名称</th><th>Agent</th><th>版本</th><th>模型</th><th>状态</th><th>基/候</th><th>正文</th><th>批准人</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!prompts.length"><td colspan="9" class="muted center">暂无 Prompt</td></tr>
          <tr v-for="p in prompts" :key="p.id">
            <td class="small">{{ p.name }}</td>
            <td><span class="tag gray">{{ p.agent_name }}</span></td>
            <td class="mono">{{ p.version }}</td>
            <td class="muted small">{{ p.model_name || '—' }}</td>
            <td><span class="tag" :class="statusTag(p.status)">{{ p.status }}</span></td>
            <td><span class="tag" :class="p.is_baseline ? 'blue' : 'green'">{{ p.is_baseline ? '基线' : '候选' }}</span></td>
            <td class="muted small reason">{{ p.content }}</td>
            <td class="small muted">{{ p.approver_id || '—' }}</td>
            <td class="actions">
              <template v-if="canManageEvaluation && p.status === 'DRAFT'">
                <button class="btn gray small-btn" @click="promptTransition(p, 'CANDIDATE')">→候选</button>
              </template>
              <template v-else-if="canManageEvaluation && p.status === 'CANDIDATE'">
                <button class="btn green small-btn" @click="promptTransition(p, 'APPROVED')">✓批准</button>
                <button class="btn red small-btn" @click="promptTransition(p, 'REJECTED')">弃</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 新建 Prompt 弹窗 -->
      <div v-if="createPromptOpen" class="modal-mask" @click.self="createPromptOpen = false">
        <div class="card modal">
          <h3>⚡ 新建 Prompt 版本（初始 DRAFT）</h3>
          <label>名称</label>
          <input v-model="promptForm.name" placeholder="例如：候选 CPS-2" />
          <label>Agent 名（同 agent 才能 A-B）</label>
          <input v-model="promptForm.agent_name" placeholder="例如：sentiment" />
          <label>模型</label>
          <input v-model="promptForm.model_name" placeholder="例如：claude-sonnet-4-5" />
          <label class="check"><input type="checkbox" v-model="promptForm.is_baseline" /> 作为基线版本</label>
          <label>正文内容</label>
          <textarea v-model="promptForm.content" rows="4" placeholder="Prompt 文本（保存后正文在批准前对团队隐藏）"></textarea>
          <div v-if="promptErr" class="error-text">{{ promptErr }}</div>
          <div class="modal-actions">
            <button class="btn" :disabled="promptSaving" @click="submitPrompt">{{ promptSaving ? '保存中…' : '保存' }}</button>
            <button class="btn gray" @click="createPromptOpen = false">取消</button>
          </div>
        </div>
      </div>
    </section>

    <!-- A-B -->
    <section v-if="curTab === 'ab'" class="section">
      <div class="section-head">
        <h3>🔬 A-B 实验（同数据集 · 人工批准）</h3>
        <div v-if="canManageEvaluation" class="head-actions">
          <button class="btn" @click="openCreateAB">+ 发起 A-B</button>
        </div>
      </div>
      <div v-if="abLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>Bundle</th><th>基线 Run</th><th>候选 Run</th><th>状态</th><th>创建时间</th><th>结果</th></tr>
        </thead>
        <tbody>
          <tr v-if="!abBundles.length"><td colspan="6" class="muted center">暂无 A-B 实验</td></tr>
          <tr v-for="b in abBundles" :key="b.bundle_id">
            <td class="mono">{{ short(b.bundle_id, 12) }}</td>
            <td class="small mono">{{ short(b.baseline?.run_id, 24) }}</td>
            <td class="small mono">{{ short(b.variant?.run_id, 24) }}</td>
            <td><span class="tag" :class="abStatusTag(b)">{{ abStatus(b) }}</span></td>
            <td class="small muted">{{ fmtTime(b.created_at) }}</td>
            <td><button class="btn green small-btn" :disabled="!abDone(b)" @click="viewABReport(b)">对比报告</button></td>
          </tr>
        </tbody>
      </table>

      <!-- 创建 A-B 弹窗 -->
      <div v-if="createABOpen" class="modal-mask" @click.self="createABOpen = false">
        <div class="card modal">
          <h3>🔬 发起 A-B 实验</h3>
          <label>名称</label>
          <input v-model="abForm.name" placeholder="例如：CPS 候选对比" />
          <label>数据集</label>
          <select v-model="abForm.suite_id">
            <option v-for="s in suites" :key="s.id" :value="s.id">{{ s.name }} (v{{ s.version }})</option>
          </select>
          <label>基线 Prompt（必须已批准）</label>
          <select v-model="abForm.baseline_id">
            <option v-for="p in aprovedBaselines" :key="p.id" :value="p.id">{{ p.name }} ({{ p.agent_name }} v{{ p.version }})</option>
          </select>
          <label>候选 Prompt（必须已批准）</label>
          <select v-model="abForm.variant_id">
            <option v-for="p in approvedCandidates" :key="p.id" :value="p.id">{{ p.name }} ({{ p.agent_name }} v{{ p.version }})</option>
          </select>
          <label>限制用例数（0 = 全量）</label>
          <input v-model.number="abForm.limit" type="number" min="0" max="300" />
          <div v-if="abErr" class="error-text">{{ abErr }}</div>
          <div class="modal-actions">
            <button class="btn" :disabled="abSaving" @click="submitAB">{{ abSaving ? '提交中…' : '发起' }}</button>
            <button class="btn gray" @click="createABOpen = false">取消</button>
          </div>
        </div>
      </div>

      <!-- A-B 报告弹窗 -->
      <div v-if="abReport" class="modal-mask" @click.self="abReport = null">
        <div class="card modal wide">
          <div class="modal-head">
            <h3>🔬 A-B 结论：{{ abReport.pass ? '✅ 可部署' : '⛔ 不可部署' }}</h3>
            <button class="btn gray small-btn" @click="abReport = null">关闭</button>
          </div>
          <div class="report-kpis">
            <div class="kpi-mini"><div class="kpi-mini-num">{{ pctText(abReport.baseline.pass_rate) }}</div><div class="kpi-mini-label">基线通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ pctText(abReport.variant.pass_rate) }}</div><div class="kpi-mini-label">候选通过率</div></div>
            <div class="kpi-mini"><div class="kpi-mini-num">{{ abReport.token_delta_pct != null ? abReport.token_delta_pct + '%' : '未测' }}</div><div class="kpi-mini-label">Token 降幅</div></div>
            <div class="kpi-mini"><div class="kpi-mini-label">目标 ≥30%</div></div>
            <div class="kpi-mini">
              <div class="kpi-mini-num" :class="abReport.quality_ok ? 'green' : 'red'">{{ abReport.quality_ok ? '✅' : '⛔' }}</div>
              <div class="kpi-mini-label">质量不劣化</div>
            </div>
          </div>
          <p class="muted small">实测：{{ abReport.measured ? '是' : '否' }}（未测量永不部署）</p>
          <p v-if="abReport.regressed.length" class="error-text">回归用例：{{ abReport.regressed.join(', ') }}</p>
          <p v-if="abReport.improved.length" class="muted small">改善用例：{{ abReport.improved.join(', ') }}</p>
        </div>
      </div>
    </section>

    <!-- Office 导入 -->
    <section v-if="curTab === 'office'" class="section">
      <div class="section-head">
        <h3>📥 Excel/CSV 导入（沙箱解析 · 人工确认）</h3>
      </div>
      <div v-if="importErr" class="error-text">{{ importErr }}</div>
      <div class="upload-card">
        <input type="file" ref="fileInput" accept=".csv,.xlsx" @change="onFileChange" />
        <div class="upload-meta muted small">FileGate 校验：仅 csv/xlsx、≤10MB、拒绝伪造扩展名与路径穿越；内容任何指令仅为数据</div>
      </div>
      <div v-if="importPreview" class="preview-box">
        <div class="preview-head">
          <h4>🔎 预览（{{ importPreview.case_count }} 条，将作为新数据集版本落库）</h4>
          <span class="tag green">{{ importPreview.file_name }}</span>
        </div>
        <label>数据集名称</label>
        <input v-model="confirmName" placeholder="例如：Office 导入-渠道对账" />
        <label>变更说明</label>
        <input v-model="confirmChangelog" placeholder="本次导入来源与说明" />
        <table class="table small">
          <thead><tr><th>Case</th><th>类型</th><th>预期决策</th><th>预期退款</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="p in importPreview.preview" :key="p.id">
              <td class="mono">{{ p.id }}</td>
              <td><span class="tag gray">{{ p.case_type }}</span></td>
              <td><span class="tag" :class="p.final_decision === 'APPROVED' ? 'green' : 'red'">{{ p.final_decision }}</span></td>
              <td><span class="tag">{{ p.refund_status }}</span></td>
              <td class="muted small reason">{{ p.reason }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn green" :disabled="importing" @click="confirmImport">{{ importing ? '确认中…' : '确认落库为数据集版本' }}</button>
          <button class="btn gray" @click="clearImport">重新选择</button>
        </div>
      </div>
    </section>

    <!-- 分级调度（smoke/core/full × manual/daily/weekly/change） -->
    <section v-if="curTab === 'schedules'" class="section">
      <div class="section-head">
        <h3>⏰ 分级调度（调度只触发 Run · 绝不自动改决策）</h3>
        <div class="head-actions">
          <span class="muted small">tier 分层 ⊆；调度触发全走人工批准的 Prompt</span>
          <button v-if="canManageEvaluation" class="btn" @click="openCreateSchedule">+ 新建调度</button>
        </div>
      </div>
      <div v-if="schedLoading" class="empty"><div class="spinner"></div></div>
      <table v-else class="table">
        <thead>
          <tr><th>数据集</th><th>层级</th><th>节奏</th><th>启用</th><th>下次到期</th><th>上次 Run</th><th>上次触发</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="!schedules.length"><td colspan="8" class="muted center">暂无调度</td></tr>
          <tr v-for="s in schedules" :key="s.id">
            <td class="small muted">{{ s.suite_name ? short(s.suite_name, 26) : short(s.suite_id, 10) }}</td>
            <td><span class="tag" :class="tierTag(s.tier)">{{ s.tier }}</span></td>
            <td><span class="tag gray">{{ s.cadence }}</span></td>
            <td><span class="tag" :class="s.enabled ? 'green' : 'gray'">{{ s.enabled ? '开' : '停' }}</span></td>
            <td class="small muted">{{ fmtTime(s.next_due_at) }}</td>
            <td class="small mono">{{ s.last_run_id ? short(s.last_run_id, 14) : '—' }}</td>
            <td class="small muted">{{ s.last_fired_at ? fmtTime(s.last_fired_at) : '—' }}</td>
            <td v-if="canManageEvaluation" class="actions">
              <button class="btn green small-btn" @click="fireSchedule(s)">▶ 触发</button>
              <button class="btn gray small-btn" @click="toggleSchedule(s)">{{ s.enabled ? '停用' : '启用' }}</button>
            </td>
            <td v-else class="muted small">只读</td>
          </tr>
        </tbody>
      </table>

      <!-- 新建调度弹窗 -->
      <div v-if="createSchedOpen" class="modal-mask" @click.self="createSchedOpen = false">
        <div class="card modal">
          <h3>⏰ 新建分级调度</h3>
          <label>数据集</label>
          <select v-model="schedForm.suite_id">
            <option v-for="s in suites" :key="s.id" :value="s.id">{{ s.name }} (v{{ s.version }})</option>
          </select>
          <label>层级 tier</label>
          <select v-model="schedForm.tier">
            <option value="smoke">smoke（冒烟 · 6 条）</option>
            <option value="core">core（核心 · 60 条）</option>
            <option value="full">full（全量 · {{ suiteFullCount }} 条）</option>
            <option value="intent">intent（意图对照 · 120 条）</option>
          </select>
          <label>节奏 cadence</label>
          <select v-model="schedForm.cadence">
            <option value="daily">每日 daily</option>
            <option value="weekly">每周 weekly</option>
            <option value="change">变更后 change</option>
            <option value="manual">手动 manual</option>
          </select>
          <label class="check"><input type="checkbox" v-model="schedForm.enabled" /> 启用（首轮立即到期待触</label>
          <div v-if="schedErr" class="error-text">{{ schedErr }}</div>
          <div class="modal-actions">
            <button class="btn" :disabled="schedSaving" @click="submitSchedule">{{ schedSaving ? '保存中…' : '保存' }}</button>
            <button class="btn gray" @click="createSchedOpen = false">取消</button>
          </div>
        </div>
      </div>
    </section>

    <div v-if="toast" class="toast">✅ {{ toast }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  evalCancelRun, evalCompareReports, evalCopySuite, evalCreateAB, evalCreatePrompt,
  evalCreateIntentEvaluation, evalCreateOneClickRun, evalCreateRun, evalCreateSchedule, evalCreateSuite, evalDeleteSuite, evalExportReport,
  evalFireSchedule, evalGetABReport, evalGetReport, evalGetRunResults, evalGetSuiteCases,
  evalGetLatestIntentEvaluation, evalGetLatestScorecard,
  evalImportConfirm, evalImportPreview, evalListAB, evalListPrompts, evalListReports,
  evalListIntentEvaluationRuns, evalListRuns, evalListSchedules, evalListSuites, evalTransitionPrompt,
  evalUpdateSchedule,
} from '../api'

const role = localStorage.getItem('role') || ''
const canManageEvaluation = computed(() => ['leader', 'admin'].includes(role))
const canAdmin = computed(() => role === 'admin')
const allTabs = [
  { key: 'datasets', icon: '🗂️', label: '数据集' },
  { key: 'intent-eval', icon: '🧭', label: '意图与商城链路' },
  { key: 'runs', icon: '⚙️', label: '任务' },
  { key: 'reports', icon: '📊', label: '报告' },
  { key: 'prompts', icon: '⚡', label: 'Prompt' },
  { key: 'ab', icon: '🔬', label: 'A-B' },
  { key: 'office', icon: '📥', label: '导入' },
  { key: 'schedules', icon: '⏰', label: '调度' },
]
const tabs = computed(() => canManageEvaluation.value ? allTabs : allTabs.filter(tab => tab.key !== 'office'))
const curTab = ref('datasets')
const online = ref(true)
const toast = ref('')
let toastTimer = null

const pendingDimensions = [
  { key: 'business_accuracy', label: '业务正确性', score: null, status: 'PENDING', measured: 0, passed: 0,
    explanation: '按评测用例最终通过数/总用例数计算。' },
  { key: 'safety_compliance', label: '安全合规性', score: null, status: 'PENDING', measured: 0, passed: 0,
    explanation: '汇总退款副作用、三项一致性核验和安全维度。' },
  { key: 'runtime_stability', label: '运行稳定性', score: null, status: 'PENDING', measured: 0, passed: 0,
    explanation: '按无降级、无执行异常的用例占比计算。' },
]
const scorecard = ref({ status: 'PENDING', run_id: null, dimensions: pendingDimensions })
const scoreDimensions = computed(() => scorecard.value.dimensions?.length ? scorecard.value.dimensions : pendingDimensions)
const oneClickBusy = ref(false)
const oneClickRunId = ref(null)

// 意图周期对照评测
const intentEvalLoading = ref(true)
const intentEvalBusy = ref(false)
const intentLatest = ref(null)
const intentDataset = ref({ version: '', sample_count: 120, checksum: '' })
const intentEvalRuns = ref([])
const intentSummary = computed(() => intentLatest.value?.summary || {})
const intentPerformance = computed(() => intentLatest.value?.performance || {})
const intentCost = computed(() => intentLatest.value?.cost || {})
const shoppingFlow = computed(() => intentSummary.value.shopping_flow || {})
const intentActiveRun = computed(() => intentEvalRuns.value.find(r => ['PENDING', 'RUNNING'].includes(r.status)))
const intentSchedule = computed(() => schedules.value.find(s => s.tier === 'intent') || null)
const intentModeText = computed(() => intentLatest.value?.config?.measurement_mode === 'live_llm' ? '真模型' : '离线代理')
const ttftMeasurementText = computed(() =>
  intentPerformance.value.ttft_ms?.measurement === 'wall_clock_live' ? '墙钟实测' : '离线估算'
)
const intentEvalButtonText = computed(() => {
  if (intentEvalBusy.value) return '正在发起…'
  if (intentActiveRun.value) return intentActiveRun.value.status === 'PENDING' ? '排队中…' : '评测中…'
  return '▶ 立即执行'
})
const intentBreakdown = computed(() => {
  const hybrid = intentSummary.value.hybrid?.per_intent || {}
  const pure = intentSummary.value.pure_llm?.per_intent || {}
  return Object.entries(hybrid).map(([intent, row]) => ({
    intent, ...row, pureRecall: pure[intent]?.recall,
  }))
})

async function loadIntentEvaluation() {
  intentEvalLoading.value = true
  try {
    const [latest, history] = await Promise.all([
      evalGetLatestIntentEvaluation(), evalListIntentEvaluationRuns(),
    ])
    intentLatest.value = latest.data.item || null
    intentDataset.value = latest.data.dataset || intentDataset.value
    intentEvalRuns.value = history.data.items || []
  } catch (e) {
    online.value = false
  } finally {
    intentEvalLoading.value = false
  }
}

async function startIntentEvaluation() {
  intentEvalBusy.value = true
  try {
    const res = await evalCreateIntentEvaluation()
    showToast(res.data.deduplicated ? '已定位到正在执行的评测' : '120 条意图 + 100 条商城退换货 OCR 评测已入队')
    await loadIntentEvaluation()
  } catch (e) {
    alert('意图评测发起失败：' + errMsg(e))
  } finally {
    intentEvalBusy.value = false
  }
}

// 数据集
const suites = ref([])
const suitesLoading = ref(true)
const viewingSuite = ref(null)
const suiteCases = ref([])
const casesLoading = ref(false)

function showToast(msg) {
  toast.value = msg
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toast.value = ''), 2600)
}

function errMsg(e) {
  return (e.response?.data?.detail) || (e.response?.data?.message) || e.message || '请求失败'
}

async function loadScorecard() {
  try {
    const res = await evalGetLatestScorecard()
    scorecard.value = res.data || { status: 'PENDING', run_id: null, dimensions: pendingDimensions }
  } catch (e) { online.value = false }
}

async function startOneClickEvaluation() {
  oneClickBusy.value = true
  try {
    const res = await evalCreateOneClickRun()
    oneClickRunId.value = res.data.run_id
    let companionStarted = false
    try {
      await evalCreateIntentEvaluation()
      companionStarted = true
    } catch (companionError) {
      console.warn('商城退换货/意图伴随评测发起失败', companionError)
    }
    curTab.value = 'runs'
    showToast(companionStarted
      ? '全量回归与100条商城退换货 OCR 评测均已入队'
      : '全量回归已入队；商城 OCR 伴随评测暂未发起')
    await loadRuns()
    await loadIntentEvaluation()
  } catch (e) {
    alert('一键评测发起失败：' + errMsg(e))
  } finally {
    oneClickBusy.value = false
  }
}

function scoreText(dimension) {
  return dimension.score == null ? 'PENDING' : `${Number(dimension.score).toFixed(1)}分`
}

function scoreClass(score) {
  if (score == null) return 'score-pending'
  if (score >= 95) return 'score-good'
  if (score >= 80) return 'score-warn'
  return 'score-risk'
}

async function loadSuites() {
  suitesLoading.value = true
  try {
    const res = await evalListSuites()
    suites.value = res.data.items || []
  } catch (e) { online.value = false } finally { suitesLoading.value = false }
}

const createSuiteOpen = ref(false)
const suiteName = ref('')
const suiteChangelog = ref('')
const suiteErr = ref('')
const suiteSaving = ref(false)

function openCreateSuite() {
  copyTarget = null
  suiteName.value = 'Golden-基线-' + new Date().toISOString().slice(0, 10)
  suiteChangelog.value = ''
  suiteErr.value = ''
  createSuiteOpen.value = true
}

let copyTarget = null
async function openCopy(s) {
  copyTarget = s
  suiteName.value = s.name + '-copy'
  suiteChangelog.value = ''
  suiteErr.value = ''
  createSuiteOpen.value = true
}

async function submitSuite() {
  if (!suiteName.value.trim()) { suiteErr.value = '请填写名称'; return }
  suiteSaving.value = true
  try {
    if (copyTarget) {
      const res = await evalCopySuite(copyTarget.id, suiteName.value, suiteChangelog.value)
      showToast(`已复制为新版本 v${res.data.item.version}`)
    } else {
      await evalCreateSuite(suiteName.value, suiteChangelog.value)
      showToast('已从 Golden 创建数据集版本')
    }
    createSuiteOpen.value = false
    copyTarget = null
    await loadSuites()
  } catch (e) { suiteErr.value = '创建失败：' + errMsg(e) }
  finally { suiteSaving.value = false }
}

async function removeSuite(s) {
  if (!confirm(`确认删除数据集「${s.name}」？被 Run 引用时将拒绝。`)) return
  try {
    await evalDeleteSuite(s.id)
    showToast('已删除')
    await loadSuites()
  } catch (e) { alert('删除失败：' + errMsg(e)) }
}

async function viewSuite(s) {
  viewingSuite.value = s
  casesLoading.value = true
  try {
    const res = await evalGetSuiteCases(s.id)
    suiteCases.value = res.data.cases || []
  } catch (e) { alert('加载用例失败：' + errMsg(e)) }
  finally { casesLoading.value = false }
}

// Runs
const runs = ref([])
const runsLoading = ref(true)
const oneClickActive = computed(() => runs.value.find(item =>
  item.run_id === oneClickRunId.value && ['PENDING', 'RUNNING'].includes(item.status)
))
const oneClickButtonText = computed(() => {
  if (oneClickBusy.value) return '正在发起…'
  if (oneClickActive.value) return `评测中 ${runPct(oneClickActive.value)}%`
  return '▶ 一键全量评测（含商城OCR）'
})
const createRunOpen = ref(false)
const runForm = ref({ name: '', suite_id: '', mode: 'in_process', limit: 0, prompt_id: '' })
const runErr = ref('')
const runSaving = ref(false)

async function loadRuns() {
  runsLoading.value = true
  try {
    const res = await evalListRuns()
    runs.value = res.data.items || []
    if (!oneClickRunId.value) {
      const activeOneClick = runs.value.find(item =>
        item.config?.one_click === true && ['PENDING', 'RUNNING'].includes(item.status)
      )
      if (activeOneClick) oneClickRunId.value = activeOneClick.run_id
    }
  } catch (e) { online.value = false } finally { runsLoading.value = false }
}

const approvedPrompts = computed(() => prompts.value.filter(p => p.status === 'APPROVED') || [])

async function openCreateRun() {
  runForm.value = { name: '', suite_id: suites.value[0]?.id || '', mode: 'in_process', limit: 0, prompt_id: '' }
  await loadPrompts()
  createRunOpen.value = true
}

async function submitRun() {
  runErr.value = ''
  if (!runForm.value.suite_id) { runErr.value = '请选择数据集'; return }
  runSaving.value = true
  try {
    const body = { name: runForm.value.name, suite_id: runForm.value.suite_id, mode: runForm.value.mode, limit: runForm.value.limit }
    if (runForm.value.prompt_id) body.prompt_id = runForm.value.prompt_id
    const res = await evalCreateRun(body)
    createRunOpen.value = false
    showToast(`Run ${res.data.run_id} 已入队`)
    setTimeout(() => loadRuns(), 1500)
  } catch (e) { runErr.value = '创建失败：' + errMsg(e) }
  finally { runSaving.value = false }
}

function canCancel(r) { return canManageEvaluation.value && ['PENDING', 'RUNNING', 'FAILED'].includes(r.status) }

async function cancelRun(r) {
  if (!confirm(`取消 Run ${r.run_id}？`)) return
  try {
    await evalCancelRun(r.run_id)
    showToast('Run 已取消')
    await loadRuns()
  } catch (e) { alert('取消失败：' + errMsg(e)) }
}

function runPct(r) {
  const sum = r.summary || {}
  if (r.status === 'COMPLETED') return 100
  if (r.status === 'FAILED') return sum.total ? Math.round(((sum.total || 0) / sum.total) * 100) : 100
  if (r.status === 'CANCELLED') return sum.total ? Math.round(((sum.total || 0) / sum.total) * 100) : 100
  return sum.total ? Math.round(((sum.passed + sum.failed + sum.degraded || 0) / sum.total) * 100) : 0
}

function rateText(r) {
  if (r.summary && r.summary.pass_rate != null) return pctText(r.summary.pass_rate)
  return '—'
}

async function viewRun(r) {
  try {
    const rep = await evalGetReport(r.run_id)
    reportDoc.value = rep.data.item || rep.data
  } catch (e) {
    // 报告未归档时退化为查看 Run 结果表
    const res = await evalGetRunResults(r.run_id)
    reportDoc.value = {
      run_id: r.run_id,
      summary: r.summary || { total: 0, passed: 0, failed: 0, degraded: 0, pass_rate: 0 },
      cases: (res.data.results || []).map(x => ({
        case_id: x.case_id, case_type: x.case_type,
        judge: { verdict: x.verdict }, degraded: x.degraded,
        execution: x.execution || {}, candidate: x.candidate,
      })),
    }
  }
}

// 报告
const reports = ref([])
const reportsLoading = ref(true)
const reportDoc = ref(null)
const compareDoc = ref(null)
const cmpA = ref('')
const cmpB = ref('')

async function loadReports() {
  reportsLoading.value = true
  try {
    const res = await evalListReports()
    reports.value = res.data.reports || []
  } catch (e) { online.value = false } finally { reportsLoading.value = false }
}

async function viewReport(r) {
  try {
    const res = await evalGetReport(r.run_id)
    reportDoc.value = res.data.item || res.data
  } catch (e) { alert('读取报告失败：' + errMsg(e)) }
}

async function doCompare() {
  if (!cmpA.value || !cmpB.value) return
  try {
    const res = await evalCompareReports(cmpA.value, cmpB.value)
    compareDoc.value = res.data
  } catch (e) { alert('对比失败：' + errMsg(e)) }
}

async function exportReport(r) {
  try {
    const res = await evalExportReport(r.run_id, 'xlsx')
    const blob = new Blob([res.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${r.run_id}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { alert('导出失败：' + errMsg(e)) }
}

// Prompt
const prompts = ref([])
const promptsLoading = ref(true)
const createPromptOpen = ref(false)
const promptForm = ref({ name: '', agent_name: '', model_name: '', content: '', is_baseline: false })
const promptErr = ref('')
const promptSaving = ref(false)

async function loadPrompts() {
  promptsLoading.value = true
  try {
    const res = await evalListPrompts()
    prompts.value = res.data.items || []
  } catch (e) { online.value = false } finally { promptsLoading.value = false }
}

function openCreatePrompt() {
  promptForm.value = { name: '', agent_name: '', model_name: '', content: '', is_baseline: false }
  createPromptOpen.value = true
}

async function submitPrompt() {
  promptErr.value = ''
  if (!promptForm.value.name || !promptForm.value.agent_name || !promptForm.value.content) {
    promptErr.value = '名称 / Agent / 正文必填'
    return
  }
  promptSaving.value = true
  try {
    const body = { ...promptForm.value }
    if (!body.model_name) delete body.model_name
    await evalCreatePrompt(body)
    createPromptOpen.value = false
    showToast('Prompt 已保存（DRAFT，待人工推进）')
    await loadPrompts()
  } catch (e) { promptErr.value = '保存失败：' + errMsg(e) }
  finally { promptSaving.value = false }
}

async function promptTransition(p, toStatus) {
  try {
    await evalTransitionPrompt(p.id, toStatus)
    showToast(`Prompt ${p.version} -> ${toStatus}`)
    await loadPrompts()
  } catch (e) { alert('状态迁移失败：' + errMsg(e)) }
}

// A-B
const abBundles = ref([])
const abLoading = ref(true)
const createABOpen = ref(false)
const abForm = ref({ name: '', suite_id: '', baseline_id: '', variant_id: '', limit: 0 })
const abErr = ref('')
const abSaving = ref(false)
const abReport = ref(null)

async function loadAB() {
  abLoading.value = true
  try {
    const res = await evalListAB()
    abBundles.value = res.data.items || []
  } catch (e) { online.value = false } finally { abLoading.value = false }
}

const aprovedBaselines = computed(() => prompts.value.filter(p => p.status === 'APPROVED' && p.is_baseline))
const approvedCandidates = computed(() => prompts.value.filter(p => p.status === 'APPROVED' && !p.is_baseline))

async function openCreateAB() {
  await Promise.all([loadSuites(), loadPrompts()])
  abForm.value = { name: '', suite_id: suites.value[0]?.id || '', baseline_id: '', variant_id: '', limit: 0 }
  createABOpen.value = true
}

async function submitAB() {
  abErr.value = ''
  if (!abForm.value.baseline_id || !abForm.value.variant_id) { abErr.value = '请选择基线/候选 Prompt' }
  abSaving.value = true
  try {
    const res = await evalCreateAB({ ...abForm.value })
    createABOpen.value = false
    showToast(`A-B 已发起：${res.data.baseline_run.run_id} / ${res.data.variant_run.run_id}`)
    setTimeout(() => loadAB(), 1500)
  } catch (e) { abErr.value = '发起失败：' + errMsg(e) }
  finally { abSaving.value = false }
}

function abStatus(b) {
  const s1 = b.baseline?.status, s2 = b.variant?.status
  if (s1 === 'COMPLETED' && s2 === 'COMPLETED') return 'COMPLETED'
  if (s1 === 'FAILED' || s2 === 'FAILED') return 'FAILED'
  return 'RUNNING'
}
function abDone(b) { return abStatus(b) === 'COMPLETED' }

async function viewABReport(b) {
  try {
    const res = await evalGetABReport(b.bundle_id)
    abReport.value = res.data
  } catch (e) { alert('A-B 报告读取失败：' + errMsg(e)) }
}

// Office 导入
const importErr = ref('')
const importPreview = ref(null)
const importing = ref(false)
const confirmName = ref('')
const confirmChangelog = ref('')
const fileInput = ref(null)

function onFileChange(e) {
  const f = e.target.files?.[0]
  if (!f) return
  importErr.value = ''
  importPreview.value = null
  uploadImport(f)
}

async function uploadImport(file) {
  try {
    const res = await evalImportPreview(file)
    importPreview.value = res.data
    confirmName.value = 'Office-导入-' + new Date().toISOString().slice(0, 10)
    confirmChangelog.value = `沙箱导入 ${importPreview.value.case_count} 条`
  } catch (e) { importErr.value = '上传失败：' + errMsg(e) }
}

async function confirmImport() {
  importing.value = true
  try {
    const res = await evalImportConfirm({
      token: importPreview.value.token, name: confirmName.value, changelog: confirmChangelog.value,
    })
    showToast(`已创建数据集版本 v${res.data.item.version}（预览文件已回收）`)
    clearImport()
    await loadSuites()
  } catch (e) { importErr.value = '确认失败：' + errMsg(e) }
  finally { importing.value = false }
}

function clearImport() {
  importPreview.value = null
  importErr.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

// 分级调度（V3.0 M18）：调度只触发 Run，绝不自动改决策
const schedules = ref([])
const schedLoading = ref(true)
const createSchedOpen = ref(false)
const schedForm = ref({ suite_id: '', tier: 'smoke', cadence: 'daily', enabled: true })
const schedErr = ref('')
const schedSaving = ref(false)

const suiteFullCount = computed(() => {
  const s = suites.value.find(x => x.id === schedForm.value.suite_id)
  return s ? s.case_count : '—'
})

async function loadSchedules() {
  schedLoading.value = true
  try {
    const res = await evalListSchedules()
    schedules.value = res.data.items || []
  } catch (e) { online.value = false } finally { schedLoading.value = false }
}

function tierTag(t) {
  return { smoke: 'blue', core: 'yellow', full: 'green', intent: 'blue' }[t] || 'gray'
}

async function openCreateSchedule() {
  if (!suites.value.length) await loadSuites()
  schedForm.value = { suite_id: suites.value[0]?.id || '', tier: 'smoke', cadence: 'daily', enabled: true }
  schedErr.value = ''
  createSchedOpen.value = true
}

async function submitSchedule() {
  schedErr.value = ''
  if (!schedForm.value.suite_id) { schedErr.value = '请选择数据集'; return }
  schedSaving.value = true
  try {
    await evalCreateSchedule(schedForm.value)
    createSchedOpen.value = false
    showToast('已新建调度（enabled 时立即到期待触发）')
    await loadSchedules()
  } catch (e) { schedErr.value = '保存失败：' + errMsg(e) }
  finally { schedSaving.value = false }
}

async function toggleSchedule(s) {
  try {
    const res = await evalUpdateSchedule(s.id, !s.enabled)
    s.enabled = res.data.item.enabled
    showToast(res.data.item.enabled ? '调度已启用' : '调度已停用')
    await loadSchedules()
  } catch (e) { alert('启停失败：' + errMsg(e)) }
}

async function fireSchedule(s) {
  if (!confirm(`手动触发 ${s.suite_name || s.suite_id} / ${s.tier}？将创建 tier Run 并入队。`)) return
  try {
    const res = await evalFireSchedule(s.id)
    showToast(`已触发 Run ${res.data.run_id}（${res.data.tier}）`)
    setTimeout(() => loadSchedules(), 800)
    if (s.tier === 'intent') setTimeout(() => loadIntentEvaluation(), 1500)
    if (curTab.value === 'schedules') setTimeout(() => loadRuns(), 1500)
  } catch (e) { alert('触发失败：' + errMsg(e)) }
}

// 工具函数
function short(s, n) { return s && s.length > n ? s.slice(0, n) + '…' : s }
function fmtTime(t) { return t ? String(t).replace('T', ' ').slice(0, 16) : '—' }
function pctText(v) { return v == null ? '—' : (v * 100).toFixed(1) + '%' }
function msText(v) { return v == null ? '—' : Number(v).toFixed(1) + ' ms' }
function formatNumber(v) { return v == null ? '—' : Number(v).toLocaleString('zh-CN') }
function cadenceText(v) { return ({ daily: '每日', weekly: '每周', manual: '手动', change: '变更触发' })[v] || v || '—' }
function metricClass(value, target, higherIsBetter) {
  if (value == null) return 'metric-neutral'
  return (higherIsBetter ? value >= target : value <= target) ? 'metric-good' : 'metric-risk'
}
function intentName(v) {
  return ({ REFUND: '退款', QUERY: '查询', EVIDENCE: '补充凭证', APPEAL: '申诉', HUMAN: '转人工', COMPLAINT_ESCALATE: '投诉升级' })[v] || v
}
function sign(v) { return v == null ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%' }
function tokenText(doc) { return doc.cost && doc.cost.input_tokens_total ? String(doc.cost.input_tokens_total) : '—' }
function evidShort(c) {
  const ev = c.judge?.evidence
  if (!ev) return '—'
  const s = ev && ev.reason ? ev.reason : JSON.stringify(ev).slice(0, 60)
  return s
}
// ---- 报告 case 业务字段（退款状态 + 核验结果）----
function biz(c) { return (c.candidate && c.candidate.business) || c.business || {} }
function rftag(s) { return { NONE: 'gray', PENDING: 'yellow', SUCCESS: 'green', FAILED: 'red' }[s] || 'gray' }
function rftext(s) { return s || '—' }
function vestag(s) { return { MATCH: 'green', MISMATCH: 'red', UNKNOWN: 'yellow' }[s] || 'gray' }
function vetext(s) { return s || '—' }
function statusTag(s) {
  return { COMPLETED: 'green', PASS: 'green', RUNNING: 'blue', PENDING: 'blue', APPROVED: 'green',
           FAILED: 'red', CANCELLED: 'gray', DRAFT: 'gray', CANDIDATE: 'yellow', REJECTED: 'red', ARCHIVED: 'blue' }[s] || 'gray'
}
function rateTag(r) {
  const pr = r.summary?.pass_rate
  if (pr == null) return 'gray'
  return pr >= 1 ? 'green' : pr >= 0.9 ? 'yellow' : 'red'
}
function verdictTag(v) { return { pass: 'green', fail: 'red', skipped: 'gray', degraded: 'yellow' }[v] || 'gray' }
const deltaCls = computed(() => {
  const d = compareDoc.value?.deltas?.pass_rate
  if (d == null) return ''
  return d >= 0 ? 'green' : 'red'
})
function abStatusTag(b) { return statusTag(abStatus(b)) }

function switchTab(k) { curTab.value = k }

function refreshAll() {
  online.value = true
  loadScorecard()
  if (curTab.value === 'datasets') loadSuites()
  else if (curTab.value === 'intent-eval') { loadIntentEvaluation(); loadSchedules() }
  else if (curTab.value === 'runs') { loadRuns(); loadSuites(); loadPrompts() }
  else if (curTab.value === 'reports') loadReports()
  else if (curTab.value === 'prompts') loadPrompts()
  else if (curTab.value === 'ab') { loadAB(); loadPrompts(); loadSuites() }
  else if (curTab.value === 'schedules') { loadSchedules(); loadSuites() }
}

let timer = null
onMounted(async () => {
  await Promise.all([loadSuites(), loadRuns(), loadReports(), loadPrompts(), loadAB(), loadSchedules(), loadScorecard(), loadIntentEvaluation()])
  // 轻量轮询：每 5s 刷新任务状态（进度/取消），不刷新报告避免干扰对比选择
  timer = setInterval(async () => {
    if (document.hidden === false) {
      if (curTab.value === 'runs' || oneClickRunId.value) await loadRuns()
      if (curTab.value === 'intent-eval' || intentActiveRun.value) await loadIntentEvaluation()
      if (oneClickRunId.value) {
        const active = runs.value.find(item => item.run_id === oneClickRunId.value)
        if (active && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(active.status)) {
          const terminalStatus = active.status
          oneClickRunId.value = null
          await Promise.all([loadScorecard(), loadReports()])
          if (terminalStatus === 'COMPLETED') showToast('一键评测已完成，三维评分已更新')
          else alert(`一键评测未完成，任务状态：${terminalStatus}`)
        }
      }
    }
  }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer); if (toastTimer) clearTimeout(toastTimer) })
</script>

<style scoped>
.eval-page { display: flex; flex-direction: column; gap: 14px; }

.eval-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 12px 18px; box-shadow: var(--shadow-sm);
}
.header-left { display: flex; align-items: center; gap: 10px; }
.eval-header h2 { font-size: 17px; letter-spacing: 0.5px; margin: 0; }
.header-badge { font-size: 11px; padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge-online { background: rgba(5, 150, 105, 0.12); color: var(--green); border: 1px solid rgba(5, 150, 105, 0.25); }
.badge-offline { background: rgba(220, 38, 38, 0.12); color: var(--red); border: 1px solid rgba(220, 38, 38, 0.25); }
.header-right { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-left: auto; align-items: center; }
.back-link { font-size: 12px; color: var(--blue); text-decoration: none; padding: 4px 10px; border: 1px solid rgba(96,165,250,0.3); border-radius: 6px; }
.back-link:hover { background: rgba(96,165,250,0.1); }

.tab-bar { display: flex; gap: 8px; flex-wrap: wrap; }
.tab {
  border: 1px solid var(--border); background: var(--panel); color: var(--muted);
  padding: 8px 16px; border-radius: 10px; font-size: 13px; cursor: pointer; transition: all 0.15s;
}
.tab.active { background: var(--grad-blue); color: #fff; border-color: transparent; box-shadow: 0 3px 10px rgba(59,130,246,0.35); }

.one-click-btn { padding: 6px 13px; font-size: 12px; white-space: nowrap; }
.scorecard-section {
  background: linear-gradient(135deg, rgba(37,99,235,0.08), rgba(14,165,233,0.03));
  border: 1px solid rgba(59,130,246,0.22); border-radius: 14px; padding: 15px 16px;
  box-shadow: var(--shadow-sm);
}
.scorecard-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 12px; }
.scorecard-heading h3 { margin: 0 0 4px; font-size: 15px; }
.scorecard-heading p { margin: 0; color: var(--muted); font-size: 11.5px; }
.score-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.score-item { background: var(--panel); border: 1px solid var(--border); border-radius: 11px; padding: 12px; }
.score-topline { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 13px; font-weight: 650; }
.score-topline strong { font-size: 18px; }
.score-good { color: var(--green); }
.score-warn { color: var(--yellow); }
.score-risk { color: var(--red); }
.score-pending { color: var(--muted); font-size: 12px !important; }
.score-track { height: 7px; background: var(--panel2); border-radius: 999px; overflow: hidden; margin: 9px 0; }
.score-track.pending { background: repeating-linear-gradient(135deg, var(--panel2), var(--panel2) 5px, var(--border) 5px, var(--border) 10px); }
.score-fill { height: 100%; border-radius: inherit; background: var(--grad-blue); transition: width 0.35s ease; }
.score-item p { min-height: 34px; margin: 0 0 5px; color: var(--muted); font-size: 11.5px; line-height: 1.45; }
.score-item small { color: var(--muted); font-size: 10.5px; }

.section { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; box-shadow: var(--shadow-sm); }
.section-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.section-head h3 { font-size: 14.5px; letter-spacing: 0.3px; margin: 0; }
.head-actions { display: flex; gap: 8px; align-items: center; }

.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table thead th { text-align: left; font-size: 11.5px; color: var(--muted); border-bottom: 1px solid var(--border); padding: 7px 8px; white-space: nowrap; }
.table tbody td { padding: 8px; border-bottom: 1px solid var(--border-light); vertical-align: middle; }
.table.small { font-size: 12.5px; }
.actions { display: flex; gap: 6px; white-space: nowrap; }
.small-btn { padding: 3px 10px; font-size: 12px; }
.progress-cell { display: flex; align-items: center; gap: 8px; min-width: 110px; }
.progress-track { flex: 1; height: 8px; background: var(--panel2); border-radius: 5px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--grad-blue); border-radius: 5px; transition: width 0.4s; }
.reason { max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.center { text-align: center; }
.mono { font-family: 'Cascadia Code', Consolas, monospace; }
.small { font-size: 12px; }
.muted { color: var(--muted); }
.empty { text-align: center; padding: 40px 16px; }
.spinner { width: 30px; height: 30px; margin: 0 auto; border: 3px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.cmp-select { width: auto; min-width: 180px; padding: 6px 10px; font-size: 12px; }

/* 弹窗 */
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { width: 460px; max-width: calc(100vw - 32px); max-height: 84vh; overflow: auto; }
.modal.wide { width: 860px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.modal-head h3 { margin: 0; font-size: 15px; }
.modal-actions { display: flex; gap: 10px; margin-top: 16px; }
.check { display: flex; align-items: center; gap: 8px; }
.check input { width: auto; }

/* 报告 KPI */
.report-kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin: 6px 0 14px; }
.kpi-mini { background: var(--panel2); border-radius: 10px; padding: 10px; text-align: center; }
.kpi-mini-num { font-size: 20px; font-weight: 750; }
.kpi-mini-num.green { color: var(--green); }
.kpi-mini-num.red { color: var(--red); }
.kpi-mini-num.yellow { color: var(--yellow); }
.kpi-mini-label { font-size: 11px; color: var(--muted); margin-top: 2px; }

.upload-card { display: flex; flex-direction: column; gap: 8px; padding: 10px 0; }
.upload-meta { font-size: 11px; }
.preview-box { margin-top: 8px; border-top: 1px solid var(--border); padding-top: 12px; }
.preview-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.preview-head h4 { margin: 0; font-size: 13.5px; }

/* 意图周期评测 */
.intent-eval-section { background: linear-gradient(145deg, var(--panel), rgba(37,99,235,0.025)); }
.intent-subtitle { margin: 5px 0 0; }
.intent-meta-bar {
  display: flex; flex-wrap: wrap; gap: 8px 22px; margin-bottom: 14px; padding: 10px 12px;
  border: 1px solid rgba(59,130,246,0.18); border-radius: 10px;
  background: rgba(59,130,246,0.055); color: var(--muted); font-size: 11.5px;
}
.intent-meta-bar b { color: var(--text); margin-right: 5px; }
.intent-kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.intent-kpi {
  min-width: 0; padding: 14px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--panel); box-shadow: 0 5px 18px rgba(15,23,42,0.035);
}
.intent-kpi-title { display: flex; justify-content: space-between; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
.target-chip { padding: 2px 7px; border-radius: 999px; color: var(--blue); background: rgba(59,130,246,0.1); font-size: 10px; white-space: nowrap; }
.intent-kpi strong { display: block; margin: 10px 0 7px; font-size: 25px; line-height: 1; color: var(--text); }
.intent-kpi strong.metric-good { color: var(--green); }
.intent-kpi strong.metric-risk { color: var(--red); }
.intent-kpi strong.metric-neutral { color: var(--muted); }
.metric-compare { color: var(--text); font-size: 11.5px; white-space: nowrap; }
.metric-compare span { color: var(--blue); padding: 0 4px; }
.intent-kpi small { display: block; min-height: 30px; margin-top: 8px; color: var(--muted); font-size: 10.5px; line-height: 1.45; }
.intent-compare-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.intent-panel { border: 1px solid var(--border); border-radius: 12px; padding: 13px 14px; background: var(--panel); }
.intent-panel.highlighted { border-color: rgba(5,150,105,0.32); background: rgba(5,150,105,0.035); }
.panel-title { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; font-weight: 700; font-size: 13px; }
.compare-list { margin: 0; }
.compare-list > div { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--border-light); }
.compare-list > div:last-child { border-bottom: 0; }
.compare-list dt { color: var(--muted); font-size: 11.5px; }
.compare-list dd { margin: 0; font-size: 12px; font-weight: 650; }
.truth-note { margin: 12px 0; padding: 10px 12px; border-left: 3px solid var(--yellow); background: rgba(245,158,11,0.065); color: var(--muted); font-size: 11px; line-height: 1.55; }
.truth-note b { color: var(--text); }
.shopping-flow-panel { margin-top: 12px; }
.shopping-flow-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.shopping-flow-grid > div { padding: 13px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel2); text-align: center; }
.shopping-flow-grid strong, .shopping-flow-grid span { display: block; }
.shopping-flow-grid strong { color: var(--text); font-size: 18px; }
.shopping-flow-grid span { margin-top: 4px; color: var(--muted); font-size: 9.5px; }
.shopping-flow-facts { display: flex; gap: 7px; margin: 12px 0 8px; flex-wrap: wrap; }
.shopping-flow-facts span { padding: 4px 8px; border: 1px solid var(--border); border-radius: 999px; color: var(--muted); background: var(--panel); font-size: 9.5px; }
.shopping-flow-error { margin-top: 10px; color: var(--red); font-size: 10.5px; }
.intent-breakdown { margin-top: 12px; padding-bottom: 5px; }
.history-head { margin-top: 20px; border-top: 1px solid var(--border); padding-top: 15px; }
.intent-empty { padding: 38px 20px; text-align: center; border: 1px dashed var(--border); border-radius: 12px; color: var(--muted); }
.intent-empty h4 { margin: 8px 0 6px; color: var(--text); }
.intent-empty p { margin: 0; font-size: 12px; }
.empty-icon { font-size: 30px; }

@media (max-width: 900px) {
  .report-kpis { grid-template-columns: repeat(3, 1fr); }
  .score-grid { grid-template-columns: 1fr; }
  .intent-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .shopping-flow-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .header-right { width: 100%; margin-left: 0; flex-wrap: wrap; }
}
@media (max-width: 620px) {
  .intent-kpi-grid, .intent-compare-grid { grid-template-columns: 1fr; }
  .shopping-flow-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
