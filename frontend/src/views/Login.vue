<template>
  <main class="login-page">
    <section class="login-hero">
      <div class="hero-brand">
        <div class="hero-logo"><AppIcon name="logo" /></div>
        <div><strong>智诉决策平台</strong><span>AI REFUND OPERATIONS</span></div>
      </div>
      <div class="hero-copy">
        <div class="hero-kicker"><span></span>多Agent协同 · V3.0治理增强版</div>
        <h1>让每一次客诉决策<br />都有依据、边界与回溯。</h1>
        <p>从订单核验、舆情分析和人工审批，到持续评测、零信任安全、意图可靠性与实时语音，统一在一套可审计的运营平台中完成。</p>
      </div>
      <div class="hero-capabilities">
        <div><AppIcon name="shield" /><span><strong>零信任安全</strong>输入、工具与资金副作用分层治理</span></div>
        <div><AppIcon name="evaluation" /><span><strong>持续评测</strong>Golden数据集、发布门禁与成本预算</span></div>
        <div><AppIcon name="mic" /><span><strong>实时交互</strong>语音会话、意图路由与客服陪练</span></div>
      </div>
      <div class="hero-footer">© 2026 Intelligent Complaint Decision Platform</div>
    </section>

    <section class="login-panel">
      <div class="login-box">
        <div class="mobile-brand">
          <div class="hero-logo"><AppIcon name="logo" /></div>
          <strong>智诉决策平台</strong>
        </div>
        <div class="login-heading">
          <span>欢迎回来</span>
          <h2>登录运营工作台</h2>
          <p>使用企业账号继续访问客诉决策与治理能力。</p>
        </div>

        <form @submit.prevent="doLogin">
          <label for="username">用户名</label>
          <div class="input-shell">
            <AppIcon name="user" />
            <input id="username" v-model.trim="username" autocomplete="username" placeholder="请输入用户名" />
          </div>
          <label for="password">密码</label>
          <div class="input-shell">
            <AppIcon name="shield" />
            <input id="password" v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="请输入密码" />
            <button type="button" class="password-toggle" @click="showPassword = !showPassword">{{ showPassword ? '隐藏' : '显示' }}</button>
          </div>
          <div v-if="err" class="login-error"><AppIcon name="warning" />{{ err }}</div>
          <button class="btn login-btn" type="submit" :disabled="loading || !username || !password">
            <span>{{ loading ? '正在验证身份…' : '安全登录' }}</span><AppIcon name="arrow" />
          </button>
        </form>

        <div class="demo-access">
          <div class="demo-head"><span>演示环境快速登录</span><span class="demo-badge">非生产</span></div>
          <button type="button" @click="fillDemo('customer_service1')"><span class="demo-avatar cs">客</span><span><strong>客服专员</strong><small>customer_service1</small></span><AppIcon name="arrow" /></button>
          <button type="button" @click="fillDemo('supervisor1')"><span class="demo-avatar sup">管</span><span><strong>业务主管</strong><small>supervisor1</small></span><AppIcon name="arrow" /></button>
          <button type="button" @click="fillDemo('leader1')"><span class="demo-avatar lead">治</span><span><strong>治理负责人</strong><small>leader1</small></span><AppIcon name="arrow" /></button>
        </div>

        <div class="eget-entry">
          <span>从这里进入</span>
          <a href="http://127.0.0.1:8002" target="_blank" rel="noopener">eget</a>
        </div>

        <div class="security-note"><AppIcon name="shield" />登录行为和高风险操作将写入不可篡改审计链。</div>
      </div>
    </section>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { login } from '../api'

const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const showPassword = ref(false)
const err = ref('')
const loading = ref(false)

function fillDemo(account) {
  username.value = account
  password.value = '123456'
  err.value = ''
}

async function doLogin() {
  err.value = ''
  loading.value = true
  try {
    const { data } = await login(username.value, password.value)
    localStorage.setItem('token', data.token)
    localStorage.setItem('username', data.username)
    localStorage.setItem('role', data.role)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(redirect)
  } catch (e) {
    const detail = e.response?.data?.detail || e.response?.data?.message
    if (!e.response) {
      err.value = '无法连接后端服务，请确认 8001 端口已经启动'
    } else if (e.response.status === 401) {
      err.value = '账号或密码错误，请重新输入'
    } else if (e.response.status === 429) {
      err.value = '登录尝试过于频繁，请稍后再试'
    } else if (e.response.status === 404) {
      err.value = '登录接口不可用，请检查后端服务版本'
    } else {
      err.value = typeof detail === 'string' ? detail : '登录服务暂时异常，请稍后重试'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page { min-height: 100vh; display: grid; grid-template-columns: minmax(520px, 1.15fr) minmax(440px, .85fr); background: #fff; }
.login-hero {
  position: relative; min-height: 100vh; display: flex; flex-direction: column; padding: 44px clamp(44px, 6vw, 92px);
  overflow: hidden; color: #fff;
  background:
    radial-gradient(540px 420px at 100% 5%, rgba(70, 117, 255, .23), transparent 70%),
    radial-gradient(420px 420px at 0 92%, rgba(19, 154, 171, .15), transparent 72%),
    linear-gradient(145deg, #091425, #0c1c34 62%, #102745);
}
.login-hero::after { content: ""; position: absolute; inset: 0; opacity: .12; background-image: linear-gradient(rgba(255,255,255,.15) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.15) 1px, transparent 1px); background-size: 48px 48px; mask-image: linear-gradient(to bottom, #000, transparent 85%); pointer-events: none; }
.hero-brand { position: relative; z-index: 1; display: flex; align-items: center; gap: 12px; }
.hero-logo { width: 42px; height: 42px; display: grid; place-items: center; color: #fff; background: linear-gradient(145deg, #4b7cf6, #3154d3); border: 1px solid rgba(255,255,255,.2); border-radius: 12px; box-shadow: 0 10px 30px rgba(31,81,198,.35); }
.hero-logo .app-icon { width: 23px; height: 23px; }
.hero-brand > div:last-child { display: flex; flex-direction: column; }
.hero-brand strong { font-size: 16px; letter-spacing: .8px; }
.hero-brand span { color: #7890b5; font-size: 9px; font-weight: 700; letter-spacing: 1.9px; }
.hero-copy { position: relative; z-index: 1; max-width: 650px; margin: auto 0 44px; }
.hero-kicker { display: flex; align-items: center; gap: 9px; color: #93b2ec; font-size: 12px; font-weight: 650; letter-spacing: .6px; }
.hero-kicker span { width: 20px; height: 2px; background: #5985ed; }
.hero-copy h1 { margin-top: 22px; font-size: clamp(38px, 4vw, 60px); line-height: 1.18; letter-spacing: -2px; }
.hero-copy p { max-width: 620px; margin-top: 24px; color: #a9bad2; font-size: 15px; line-height: 1.85; }
.hero-capabilities { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.hero-capabilities > div { min-height: 105px; padding: 16px; background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.075); border-radius: 13px; backdrop-filter: blur(5px); }
.hero-capabilities .app-icon { width: 19px; height: 19px; color: #82a7ff; }
.hero-capabilities span { display: flex; flex-direction: column; margin-top: 12px; color: #8094b0; font-size: 10px; line-height: 1.55; }
.hero-capabilities strong { color: #d9e6f8; font-size: 12px; }
.hero-footer { position: relative; z-index: 1; margin-top: 34px; color: #526985; font-size: 10px; }
.login-panel { display: grid; place-items: center; padding: 42px; background: radial-gradient(520px 260px at 50% 0, #f3f7ff, transparent 75%), #fff; }
.login-box { width: min(410px, 100%); }
.mobile-brand { display: none; }
.login-heading > span { color: var(--blue); font-size: 11px; font-weight: 700; letter-spacing: 1px; }
.login-heading h2 { margin-top: 6px; color: var(--text-strong); font-size: 28px; letter-spacing: -.6px; }
.login-heading p { margin-top: 7px; color: var(--muted); font-size: 13px; }
form { margin-top: 30px; }
.input-shell { position: relative; display: flex; align-items: center; }
.input-shell > .app-icon { position: absolute; left: 12px; z-index: 1; width: 17px; height: 17px; color: #8794a7; }
.input-shell input { height: 44px; padding-left: 40px; padding-right: 60px; background: #f9fbfd; }
.password-toggle { position: absolute; right: 10px; padding: 4px; color: var(--blue); background: transparent; border: 0; cursor: pointer; font-size: 11px; }
.login-btn { width: 100%; height: 44px; justify-content: space-between; margin-top: 20px; padding: 0 16px 0 19px; }
.login-error { display: flex; align-items: center; gap: 6px; margin-top: 10px; padding: 9px 10px; color: var(--red); background: #fff2f3; border-radius: 8px; font-size: 12px; }
.login-error .app-icon { width: 15px; }
.demo-access { margin-top: 25px; padding-top: 22px; border-top: 1px solid var(--border); }
.demo-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; color: var(--muted); font-size: 11px; }
.demo-badge { padding: 2px 6px; color: #a65300; background: #fff5e7; border-radius: 5px; font-size: 9px; font-weight: 700; }
.demo-access > button { width: 100%; display: flex; align-items: center; gap: 10px; margin-top: 7px; padding: 9px 10px; color: var(--text); background: #fff; border: 1px solid var(--border); border-radius: 9px; cursor: pointer; text-align: left; transition: background .15s, border-color .15s; }
.demo-access > button:hover { background: #f8faff; border-color: #b8c9ef; }
.demo-access > button > span:nth-child(2) { display: flex; flex: 1; flex-direction: column; }
.demo-access strong { font-size: 12px; }
.demo-access small { color: var(--muted); font-size: 10px; }
.demo-access button > .app-icon { width: 14px; height: 14px; color: #91a0b5; }
.demo-avatar { width: 31px; height: 31px; display: grid; place-items: center; color: #fff; border-radius: 8px; font-size: 11px; font-weight: 700; }
.demo-avatar.cs { background: #3971e8; }.demo-avatar.sup { background: #7852cf; }
.demo-avatar.lead { background: #087c76; }
.security-note { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 24px; color: #8a96a8; font-size: 10px; }
.security-note .app-icon { width: 13px; height: 13px; }
.eget-entry { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 18px; padding: 11px; background: #f6f8ff; border: 1px dashed #c4d2f5; border-radius: 10px; color: var(--muted); font-size: 12px; }
.eget-entry a { color: var(--blue); font-size: 14px; font-weight: 750; letter-spacing: .5px; }
.eget-entry a:hover { color: var(--blue-dark); text-decoration: underline; }
@media (max-width: 980px) {
  .login-page { grid-template-columns: 1fr; }
  .login-hero { display: none; }
  .login-panel { min-height: 100vh; padding: 28px 20px; }
  .mobile-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 44px; }
  .mobile-brand strong { font-size: 16px; }
}
</style>
