/**
 * 用户 Store：当前登录用户的资料（GET /users/me）。
 *
 * 与 stores/auth.ts 的分工：auth 管 Token，user 管「我是谁」。
 * 本文件由脚手架占位实现（``username = ref('Guest')``）改造为真实实现，
 * 原有的 ``useUserStore`` 名称与文件位置保持不变。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchCurrentUser, type User } from '../api/user'
import { errorMessage } from '../utils/error'

export const useUserStore = defineStore('user', () => {
  /** 当前登录用户；未登录 / 未加载时为 null */
  const user = ref<User | null>(null)

  /** 请求进行中 */
  const loading = ref(false)

  /** 已归一化的错误文案（null 表示无错误） */
  const error = ref<string | null>(null)

  /** 展示名称（顶栏使用） */
  const displayName = computed(() => user.value?.username ?? '未登录')

  /**
   * 当前用户是否管理员。
   *
   * 用途：前端菜单 / 路由的可见性控制（体验层）。
   * **不能替代后端权限**：管理接口仍由后端 ``require_admin`` 独立校验
   * （普通用户调用会收到 403）。
   */
  const isAdmin = computed(() => user.value?.role === 'admin')

  /**
   * 加载当前登录用户信息。
   *
   * Token 无效时后端返回 401，由 utils/request.ts 统一处理
   * （清 token + 跳回登录页），此处只记录错误并继续向上抛出。
   */
  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      user.value = await fetchCurrentUser()
    } catch (err) {
      user.value = null
      error.value = errorMessage(err)
      throw err
    } finally {
      loading.value = false
    }
  }

  /** 清空用户资料（登出 / Token 失效时调用） */
  function reset(): void {
    user.value = null
    error.value = null
  }

  return {
    user,
    loading,
    error,
    displayName,
    isAdmin,
    load,
    reset,
  }
})
