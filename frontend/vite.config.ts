import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    vueDevTools(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // Enterprise-AI-Agent-Platform 固定开发端口 5174
    // （5173 属于另一个项目 Travel-H5，两个项目需要同时运行）
    port: 5174,
    // 端口被占用时直接报错退出，不自动切换到 5175
    strictPort: true,
  },
})
