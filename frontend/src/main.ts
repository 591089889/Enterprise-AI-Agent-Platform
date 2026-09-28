/**
 * 应用入口。
 *
 * 顺序很重要：先安装 Pinia（store 的初始状态来自 localStorage），
 * 再安装 Router（路由守卫会读取 auth store）。
 */

import './assets/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import { useChatStore } from './stores/chat'
import { useConversationStore } from './stores/conversation'
import { useUserStore } from './stores/user'
import { setUnauthorizedHandler } from './utils/request'

const app = createApp(App)

app.use(createPinia())
app.use(router)

// ------------------------------------------------------------------
// 401 统一处理（Stage 10 统一错误结构中的 AUTHENTICATION_REQUIRED）
// ------------------------------------------------------------------
// 任意请求收到 401 时：
//   1. 清理本地 token（request.ts 已经清掉 localStorage 中的那一份）；
//   2. 清空内存中的用户 / 会话 / 聊天状态；
//   3. 回到登录页，并记录来源路径以便登录后跳回。
//
// 在这里注册（而不是写在 request.ts 里）是为了避免
// utils/request.ts ↔ stores 之间的循环依赖。
setUnauthorizedHandler(() => {
  useAuthStore().clear()
  useUserStore().reset()
  useConversationStore().reset()
  useChatStore().reset()

  const current = router.currentRoute.value
  if (current.name !== 'login') {
    void router.replace({
      name: 'login',
      query: { redirect: current.fullPath },
    })
  }
})

app.mount('#app')
