<template>
  <router-view v-if="!isLoggedIn" v-slot="{ Component, route: viewRoute }">
    <PageErrorBoundary :key="viewRoute.fullPath">
      <Suspense>
        <component :is="Component" />
        <template #fallback><div class="card route-loading">页面加载中…</div></template>
      </Suspense>
    </PageErrorBoundary>
  </router-view>

  <div v-else class="app-shell" :class="{ 'sidebar-collapsed': collapsed }">
    <div v-if="mobileOpen" class="nav-backdrop" @click="mobileOpen = false"></div>
    <aside class="app-sidebar" :class="{ 'mobile-open': mobileOpen }">
      <div class="brand">
        <div class="brand-mark"><AppIcon name="logo" /></div>
        <div class="brand-copy">
          <strong>智诉决策平台</strong>
          <span>AI REFUND OPS</span>
        </div>
        <button class="icon-btn mobile-close" aria-label="关闭导航" @click="mobileOpen = false">
          <AppIcon name="close" />
        </button>
      </div>

      <nav class="side-nav" aria-label="主导航">
        <div v-for="group in visibleNavigation" :key="group.label" class="nav-group">
          <div class="nav-group-label">{{ group.label }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            class="nav-item"
            :title="collapsed ? item.label : undefined"
            @click="mobileOpen = false"
          >
            <AppIcon :name="item.icon" />
            <span>{{ item.label }}</span>
            <span v-if="item.badge" class="nav-badge">{{ item.badge }}</span>
          </router-link>
        </div>
      </nav>

      <div class="sidebar-footer">
        <div class="system-health">
          <span class="health-dot"></span>
          <div>
            <strong>服务运行正常</strong>
            <span>V3.0 · 治理增强版</span>
          </div>
        </div>
        <button class="collapse-btn" @click="collapsed = !collapsed">
          <AppIcon name="chevron" />
          <span>收起导航</span>
        </button>
      </div>
    </aside>

    <div class="app-stage">
      <header class="app-topbar">
        <div class="topbar-left">
          <button class="icon-btn mobile-menu" aria-label="打开导航" @click="mobileOpen = true">
            <AppIcon name="menu" />
          </button>
          <div class="breadcrumb">
            <span>智诉决策平台</span>
            <AppIcon name="chevron" />
            <strong>{{ route.meta.title || '工作台' }}</strong>
          </div>
        </div>
        <div class="topbar-right">
          <span class="environment-pill"><span></span>安全演示环境</span>
          <router-link class="icon-btn" to="/search" aria-label="全局检索" title="全局检索">
            <AppIcon name="search" />
          </router-link>
          <div class="topbar-divider"></div>
          <div class="user-summary">
            <div class="avatar">{{ userInitial }}</div>
            <div class="user-copy">
              <strong>{{ username }}</strong>
              <span>{{ roleLabel }}</span>
            </div>
          </div>
          <button class="icon-btn logout" aria-label="退出登录" title="退出登录" @click="logout">
            <AppIcon name="logout" />
          </button>
        </div>
      </header>

      <main class="app-content">
        <router-view v-slot="{ Component, route: viewRoute }">
          <PageErrorBoundary :key="viewRoute.fullPath">
            <Suspense>
              <component :is="Component" />
              <template #fallback><div class="card route-loading">页面加载中…</div></template>
            </Suspense>
          </PageErrorBoundary>
        </router-view>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from './components/AppIcon.vue'
import PageErrorBoundary from './components/PageErrorBoundary.vue'

const route = useRoute()
const router = useRouter()
const collapsed = ref(localStorage.getItem('sidebar_collapsed') === '1')
const mobileOpen = ref(false)

const username = computed(() => localStorage.getItem('username') || '未登录用户')
const role = computed(() => localStorage.getItem('role') || '')
const isLoggedIn = computed(() => {
  route.fullPath
  return Boolean(localStorage.getItem('token'))
})
const userInitial = computed(() => (username.value[0] || 'U').toUpperCase())
const roleLabel = computed(() => ({
  supervisor: '业务主管',
  leader: '治理负责人',
  admin: '系统管理员',
  customer_service: '客服专员',
  staff: '客服专员',
}[role.value] || role.value || '平台用户'))

const navigation = [
  {
    label: '业务处理',
    items: [
      { to: '/', label: '工单总览', icon: 'grid' },
      { to: '/workspace', label: '协同工作台', icon: 'users' },
      { to: '/search', label: '全局检索', icon: 'search' },
    ],
  },
  {
    label: '智能治理',
    items: [
      { to: '/eval-center', label: '评测中心', icon: 'evaluation', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/security', label: '安全运营', icon: 'shield', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/intent', label: '意图可靠性', icon: 'route', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/voice', label: '语音与陪练', icon: 'mic' },
      { to: '/governance', label: '持续治理', icon: 'governance', roles: ['leader', 'admin'] },
    ],
  },
  {
    label: '运营管理',
    items: [
      { to: '/monitor/quality', label: '质量看板', icon: 'chart', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/monitor', label: '实时监控', icon: 'activity', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/platform', label: '发布与对账', icon: 'reconcile', roles: ['supervisor', 'leader', 'admin'] },
      { to: '/policy', label: '策略中心', icon: 'settings', roles: ['leader', 'admin'] },
    ],
  },
]

const visibleNavigation = computed(() => navigation
  .map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.roles || item.roles.includes(role.value)),
  }))
  .filter((group) => group.items.length))

watch(collapsed, (value) => localStorage.setItem('sidebar_collapsed', value ? '1' : '0'))
watch(() => route.fullPath, () => { mobileOpen.value = false })

function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  localStorage.removeItem('role')
  router.replace('/login')
}
</script>
