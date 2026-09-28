/**
 * 路由表 + 登录守卫。
 *
 * 平台路由结构
 * ------------
 *
 *     /login                             登录（注册入口也在这里，独立于平台外壳）
 *     /app                               MainLayout（企业平台外壳）
 *       ├── /app/chat[/:conversationId]  AI 工作台（ChatView）
 *       ├── /app/agents                  Agent 管理（AgentManagementView，需管理员）
 *       ├── /app/knowledge               知识库（占位页面）
 *       └── /app/admin                   系统管理（占位页面）
 *
 * 兼容与兜底：
 * - 旧地址 ``/chat`` 与 ``/chat/:conversationId`` 重定向到 ``/app/chat`` 体系；
 * - 未知路径统一回到主界面（未登录时再由守卫转到登录页）。
 *
 * 守卫职责
 * --------
 * 1. 受保护页面需要「持有 Token」才能进入（Token 是否有效由后端判定，
 *    401 → request.ts 统一清理并回到登录页）；
 * 2. ``meta.requiresAdmin`` 的页面需要当前用户 ``role === 'admin'``：
 *    非管理员访问时重定向回 AI 工作台并带上 ``?denied=agents``
 *    （由 MainLayout 展示无权限提示）。
 *
 * 注意：前端守卫只是**体验层**控制，管理接口的访问控制由后端
 * ``require_admin`` 独立强制（普通用户调用仍然 403）。
 *
 * 说明：``stores/auth.ts`` 的初始 token 来自 localStorage，
 * 因此刷新页面后不会误判为未登录（也就不会把用户踢回登录页）。
 */

import { createRouter, createWebHistory } from 'vue-router'

import MainLayout from '../layouts/MainLayout.vue'
import { useAuthStore } from '../stores/auth'
import { useUserStore } from '../stores/user'
import AdminView from '../views/AdminView.vue'
import AgentManagementView from '../views/AgentManagementView.vue'
import ChatView from '../views/ChatView.vue'
import KnowledgeBaseView from '../views/KnowledgeBaseView.vue'
import LoginView from '../views/LoginView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      redirect: { name: 'chat' },
    },
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { title: '登录' },
    },
    {
      // 企业平台外壳：所有 /app/* 页面共用 MainLayout（顶栏 + 模块导航 + 内容区）
      // requiresAuth 只声明在父路由上，守卫通过 to.matched 向上查找
      path: '/app',
      component: MainLayout,
      meta: { requiresAuth: true },
      children: [
        {
          // /app → /app/chat
          path: '',
          redirect: { name: 'chat' },
        },
        {
          path: 'chat/:conversationId?',
          name: 'chat',
          component: ChatView,
          meta: { title: 'AI 工作台' },
        },
        {
          path: 'agents',
          name: 'agents',
          component: AgentManagementView,
          // 企业管理功能：仅管理员可进入（后端 /agents 接口同样要求 admin）
          meta: { title: 'Agent 管理', requiresAdmin: true },
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: KnowledgeBaseView,
          // 企业管理功能：仅管理员可进入（后端 /knowledge-bases 接口同样要求 admin）
          meta: { title: '知识库', requiresAdmin: true },
        },
        {
          path: 'admin',
          name: 'admin',
          component: AdminView,
          // 平台概览 Dashboard：仅管理员可进入
          // （后端 GET /admin/dashboard 同样要求 admin：普通用户 403）
          meta: { title: '系统管理', requiresAdmin: true },
        },
      ],
    },
    {
      // 兼容旧地址：/chat[/:conversationId] → /app/chat[/:conversationId]
      path: '/chat/:conversationId?',
      redirect: (to) => {
        const raw = to.params.conversationId
        const value = Array.isArray(raw) ? raw[0] : raw
        return value ? `/app/chat/${value}` : '/app/chat'
      },
    },
    {
      // 未知路径统一回到主界面（未登录时由守卫转到登录页）
      path: '/:pathMatch(.*)*',
      redirect: { name: 'chat' },
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // requiresAuth 声明在 /app 父路由上（嵌套路由下 meta 会合并），
  // 这里用 matched 向上查找，避免依赖 meta 合并的具体细节。
  const requiresAuth = to.matched.some(
    (record) => record.meta.requiresAuth === true,
  )

  // 未登录访问受保护页面 → 登录页（记录来源，登录后跳回）
  if (requiresAuth && !auth.isAuthenticated) {
    return {
      name: 'login',
      query: { redirect: to.fullPath },
    }
  }

  // 已登录再访问登录页 → 主界面
  if (to.name === 'login' && auth.isAuthenticated) {
    return { name: 'chat' }
  }

  // 管理员区域（meta.requiresAdmin）：需要 role === 'admin'
  const requiresAdmin = to.matched.some(
    (record) => record.meta.requiresAdmin === true,
  )
  if (requiresAdmin) {
    const user = useUserStore()

    // 首次进入 / 刷新页面时用户资料可能尚未加载 → 先加载一次
    // （401 由 utils/request.ts 统一处理：清 token + 回登录页）
    if (!user.user) {
      try {
        await user.load()
      } catch {
        // 加载失败（网络/401）：下面按「非管理员」处理，不阻塞导航
      }
    }

    if (!user.isAdmin) {
      // 带上目标模块名（agents / knowledge ...），由 MainLayout 展示无权限提示
      return { name: 'chat', query: { denied: String(to.name ?? '') } }
    }
  }

  return true
})

export default router
