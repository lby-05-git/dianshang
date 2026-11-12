<template>
  <div class="nf-wrap">
    <div class="card nf-card">
      <div class="nf-code">404</div>
      <div class="nf-text">页面不存在或已失效</div>
      <div class="nf-hint">你访问的链接可能已过期</div>
      <a class="btn" href="#/">← 返回工作台</a>
    </div>
  </div>
</template>

<style scoped>
.nf-wrap {
  min-height: calc(100vh - 140px);
  display: flex; align-items: center; justify-content: center;
}
.nf-card { text-align: center; padding: 40px 60px; }
.nf-code {
  font-size: 52px; font-weight: 800; color: var(--blue);
  font-family: Consolas, monospace; line-height: 1;
}
.nf-text { font-size: 17px; font-weight: 600; margin-top: 12px; }
.nf-hint { color: var(--muted); font-size: 13px; margin: 6px 0 20px; }
</style>
