import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const canonicalLocalHost = {
  name: 'canonical-local-host',
  configureServer(server) {
    server.middlewares.use((req, res, next) => {
      if ((req.headers.host || '').toLowerCase().startsWith('localhost:')) {
        res.statusCode = 302
        res.setHeader('Location', 'http://127.0.0.1:5173' + (req.url || '/'))
        res.end()
        return
      }
      next()
    })
  },
}

// 兼容旧页面的开发代理；当前 API 封装在开发环境默认直连 127.0.0.1:8001。
export default defineConfig({
  plugins: [vue(), canonicalLocalHost],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true },
    },
  },
})
