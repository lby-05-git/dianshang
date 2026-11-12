import { build, createServer, preview } from 'vite'
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

const command = process.argv[2] || 'dev'
const shared = {
  configFile: false,
  root: process.cwd(),
  plugins: [vue(), canonicalLocalHost],
}

if (command === 'build') {
  await build({
    ...shared,
    build: {
      chunkSizeWarningLimit: 1200,
    },
  })
} else if (command === 'preview') {
  const server = await preview({
    ...shared,
    preview: {
      host: '127.0.0.1',
      port: 4173,
    },
  })
  server.printUrls()
} else {
  const server = await createServer({
    ...shared,
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8001',
          changeOrigin: true,
        },
      },
    },
  })
  await server.listen()
  server.printUrls()
}
