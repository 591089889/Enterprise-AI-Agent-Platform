<script setup lang="ts">
/**
 * 企业平台统一外壳（MainLayout）。
 *
 * 职责边界
 * --------
 * 只负责「平台级」的三件事：
 *
 *   1. 顶栏：品牌名 + 当前登录用户 + 退出登录；
 *   2. 左侧模块导航（``components/Sidebar.vue``）；
 *   3. 右侧内容区（``<router-view />``，渲染具体业务页面）。
 *
 * 不包含任何业务逻辑：Chat / Agent 管理 / 知识库 / 系统管理 全部在各自页面里，
 * 本组件不 import 任何 ``api/*.ts``。
 *
 * 说明：登录页 ``/login`` 是 ``/app`` 的兄弟路由，不使用本布局。
 */

import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import Sidebar from '../components/Sidebar.vue'
import { useAdminStore } from '../stores/admin'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useConversationStore } from '../stores/conversation'
import { useDashboardStore } from '../stores/dashboard'
import { useUserStore } from '../stores/user'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const user = useUserStore()
const conversations = useConversationStore()
const chat = useChatStore()
const dashboard = useDashboardStore()
const admin = useAdminStore()

/**
 * 被路由守卫拦截后的提示（见 ``router/index.ts``：非管理员访问管理模块时
 * 重定向到 ``/app/chat?denied={模块名}``，例如 ``denied=agents`` / ``denied=knowledge``）。
 */
const accessDenied = computed(
  () => typeof route.query.denied === 'string' && route.query.denied !== '',
)

onMounted(async () => {
  // 顶栏需要展示当前用户；用户资料只在首次进入平台时加载一次。
  // Token 失效 → 401 → utils/request.ts 统一清状态并跳回登录页。
  if (!user.user) {
    try {
      await user.load()
    } catch {
      // 错误已写入 user.error（由下方提示展示）；401 由全局处理器接管
    }
  }
})

/** 退出登录：清空全部前端状态并回到登录页（与既有行为一致，仅从 ChatView 移到外壳） */
async function onLogout(): Promise<void> {
  auth.clear()
  user.reset()
  conversations.reset()
  chat.reset()
  dashboard.reset()
  admin.reset()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div>
        <h1 class="app-header__title">Enterprise AI Agent Platform</h1>
        <p class="app-header__subtitle">企业 AI Agent 平台</p>
      </div>
      <div class="app-header__actions">
        <span class="user-chip">{{ user.displayName }}</span>
        <button class="btn btn--ghost" type="button" @click="onLogout">
          退出登录
        </button>
      </div>
    </header>

    <p v-if="accessDenied" class="page-alert">
      <span class="alert alert--error">
        没有权限访问该模块（需要管理员权限）
      </span>
    </p>

    <p v-if="user.error" class="page-alert">
      <span class="alert alert--error">{{ user.error }}</span>
    </p>

    <div class="app-layout">
      <Sidebar />
      <main class="main-content">
        <router-view />
      </main>
    </div>
  </div>
</template>
