<template>
  <slot v-if="!errorMessage" />
  <div v-else class="page-error card" role="alert">
    <div class="page-error-icon">!</div>
    <h2>页面没有正常加载</h2>
    <p>{{ errorMessage }}</p>
    <div class="page-error-actions">
      <button class="btn" @click="reload">重新加载</button>
      <button class="btn gray" @click="goHome">返回工单总览</button>
    </div>
  </div>
</template>

<script setup>
import { onErrorCaptured, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const errorMessage = ref('')

onErrorCaptured((error) => {
  console.error('[page-render-error]', error)
  errorMessage.value = '页面组件出现异常。您可以重新加载；若问题仍然存在，请记录当前页面名称。'
  return false
})

function reload() {
  window.location.reload()
}

function goHome() {
  errorMessage.value = ''
  router.push('/')
}
</script>

<style scoped>
.page-error {
  min-height: 320px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 44px 24px;
}
.page-error-icon {
  width: 54px;
  height: 54px;
  display: grid;
  place-items: center;
  border-radius: 16px;
  color: var(--red);
  background: rgba(248, 113, 113, 0.12);
  border: 1px solid rgba(248, 113, 113, 0.28);
  font-size: 28px;
  font-weight: 800;
}
.page-error h2 { margin: 16px 0 8px; font-size: 20px; }
.page-error p { margin: 0; color: var(--muted); max-width: 520px; }
.page-error-actions { display: flex; gap: 10px; margin-top: 20px; }
</style>
