import { createRouter, createWebHashHistory } from 'vue-router'
import Login from './views/Login.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: Login, meta: { title: '登录' } },
    { path: '/', component: () => import('./views/Dashboard.vue'), meta: { title: '工单总览' } },
    { path: '/cases/:id', component: () => import('./views/CaseDetail.vue'), meta: { title: '工单详情' } },
    { path: '/workspace', component: () => import('./views/CollaborativeInbox.vue'), meta: { title: '协同工作台' } },
    { path: '/search', component: () => import('./views/AdvancedSearch.vue'), meta: { title: '全局检索' } },
    { path: '/eval-center', component: () => import('./views/EvaluationCenter.vue'), meta: { title: '评测中心', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/security', component: () => import('./views/SecurityCenter.vue'), meta: { title: '安全运营', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/intent', component: () => import('./views/IntentCenter.vue'), meta: { title: '意图可靠性', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/voice', component: () => import('./views/VoiceCenter.vue'), meta: { title: '语音与陪练' } },
    { path: '/governance', component: () => import('./views/Governance.vue'), meta: { title: '持续治理', roles: ['leader', 'admin'] } },
    { path: '/monitor/quality', component: () => import('./views/QualityDashboard.vue'), meta: { title: '质量看板', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/monitor', component: () => import('./views/Monitor.vue'), meta: { title: '实时监控', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/platform', component: () => import('./views/PlatformCenter.vue'), meta: { title: '发布与对账', roles: ['supervisor', 'leader', 'admin'] } },
    { path: '/policy', component: () => import('./views/PolicyCenter.vue'), meta: { title: '策略中心', roles: ['leader', 'admin'] } },
    { path: '/:pathMatch(.*)*', component: () => import('./views/NotFound.vue'), meta: { title: '页面不存在' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  const token = localStorage.getItem('token')
  if (!token && to.path !== '/login') {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (token && to.path === '/login') return '/'
  const allowedRoles = to.meta.roles
  const role = localStorage.getItem('role')
  if (token && Array.isArray(allowedRoles) && !allowedRoles.includes(role)) return '/'
})

router.afterEach((to) => {
  document.title = (to.meta.title ? to.meta.title + ' · ' : '') + '智诉决策平台'
  sessionStorage.removeItem(`route-import-retry:${to.fullPath}`)
})

router.onError((error, to) => {
  console.error('[router-error]', error)
  const message = String(error?.message || error)
  const chunkFailure = /dynamically imported module|module script|loading chunk|failed to fetch/i.test(message)
  if (!chunkFailure) return

  const retryKey = `route-import-retry:${to.fullPath}`
  if (!sessionStorage.getItem(retryKey)) {
    sessionStorage.setItem(retryKey, '1')
    window.location.reload()
  }
})

export default router
