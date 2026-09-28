/**
 * 认证 Store：只负责「登录态（Token）」本身。
 *
 * 职责边界
 * --------
 * - Token 的读写与持久化（localStorage，key 见 utils/authToken.ts）；
 * - 调用 POST /auth/login 获取 Token；
 * - 清除登录态（用户主动登出、或 401 被动失效）。
 *
 * 用户资料（GET /users/me）由 stores/user.ts 负责，两者互不耦合。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { login as loginApi } from '../api/auth'
import { clearToken, getToken, setToken } from '../utils/authToken'

export const useAuthStore = defineStore('auth', () => {
  /** 当前 access token（初始值来自 localStorage，因此刷新页面后仍保持登录） */
  const token = ref<string | null>(getToken())

  /** 登录请求进行中（登录按钮 loading / disabled 依据） */
  const loading = ref(false)

  /** 是否已持有 Token（路由守卫据此判断能否访问受保护页面） */
  const isAuthenticated = computed(
    () => token.value !== null && token.value !== '',
  )

  /**
   * 登录：POST /auth/login → 保存 access_token。
   *
   * 失败时抛出 ApiError（由调用方决定如何展示 message）。
   */
  async function login(email: string, password: string): Promise<void> {
    loading.value = true
    try {
      const { access_token } = await loginApi({ email, password })
      token.value = access_token
      setToken(access_token)
    } finally {
      loading.value = false
    }
  }

  /** 清除登录态：用户主动登出 / 401 被动失效（同时清理 localStorage） */
  function clear(): void {
    token.value = null
    clearToken()
  }

  return {
    token,
    loading,
    isAuthenticated,
    login,
    clear,
  }
})
