/**
 * Dashboard Store：平台概览统计（Stage 18）。
 *
 * 数据来源：``GET /admin/dashboard``（仅管理员，后端 ``require_admin`` 强制）。
 *
 * 设计原则（与 stores/agent.ts / stores/conversation.ts 保持一致）
 * ---------------------------------------------------------------
 * - 页面只读 store，不直接调用 ``api/*``；
 * - 错误在 store 内归一化为「可直接展示的文案」（``utils/error.ts``）；
 * - **不伪造数据**：加载失败时 ``stats`` 保持为 ``null``，
 *   页面只展示错误提示，不显示任何假数字。
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchDashboardStats, type DashboardStats } from '../api/admin'
import { errorMessage } from '../utils/error'

export const useDashboardStore = defineStore('dashboard', () => {
  /** 概览统计；未加载成功时为 null（页面据此显示加载中 / 错误） */
  const stats = ref<DashboardStats | null>(null)

  /** 请求进行中（刷新按钮禁用依据） */
  const loading = ref(false)

  /** 已归一化的错误文案（null 表示无错误） */
  const error = ref<string | null>(null)

  /**
   * 加载平台概览统计。
   *
   * 失败时不抛出（错误写入 ``error``，401 由 ``utils/request.ts`` 统一处理），
   * 但会**清空旧数据之外不做任何填充**，避免出现与实际不符的数字。
   */
  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      stats.value = await fetchDashboardStats()
    } catch (err) {
      error.value = errorMessage(err)
      stats.value = null
    } finally {
      loading.value = false
    }
  }

  /** 清空全部状态（登出时调用） */
  function reset(): void {
    stats.value = null
    loading.value = false
    error.value = null
  }

  return {
    stats,
    loading,
    error,
    load,
    reset,
  }
})
