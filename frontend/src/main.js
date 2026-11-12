import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

if (import.meta.env.DEV && (location.hostname !== '127.0.0.1' || location.port !== '5173')) {
  location.replace('http://127.0.0.1:5173' + location.pathname + location.search + location.hash)
} else {
  createApp(App).use(router).mount('#app')
}
