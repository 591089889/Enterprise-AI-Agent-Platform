/**
 * Admin 用户管理 Store（Stage 19）。
 *
 * 职责
 * ----
 * 管理员**用户管理**区域的唯一状态源：
 *
 *     用户列表（后端分页） + 修改角色 + 删除用户
 *
 * 设计原则（与 stores/agent.ts / stores/knowledgeBase.ts 保持一致）
 * ---------------------------------------------------------------
 * - 页面只读 store，不直接调用 ``api/*``；
 * - 错误在 store 内归一化为「可直接展示的文案」（``utils/error.ts``）；
 * - ``loading`` 列表加载中；``submitting`` 写操作进行中（按钮禁用）；
 * - 写操作成功后**重新拉取当前页**，保证前端状态与服务器一致
 *   （后端删除返回 204 无响应体，本地删改容易与服务端漂移）；
 * - **不在前端实现权限业务**：能否改角色 / 能否删除由后端
 *   （``require_admin`` + AdminService 规则）判定，前端只负责展示与反馈。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  deleteAdminUser,
  fetchAdminUsers,
  updateAdminUserRole,
  type UserRole,
} from '../api/admin'
import type { User } from '../api/user'
import { errorMessage } from '../utils/error'

/** 默认每页数量（与后端 ``page_size`` 默认值一致；上限 100） */
export const DEFAULT_PAGE_SIZE = 20

export const useAdminStore = defineStore('admin', () => {
  /** 当前页用户列表（来自 ``GET /admin/users``，不写死数据） */
  const users = ref<User[]>([])
  /** 用户总数（后端返回，用于分页信息） */
  const total = ref(0)
  /** 当前页码（从 1 开始） */
  const page = ref(1)
  /** 每页数量 */
  const pageSize = ref(DEFAULT_PAGE_SIZE)

  /** 列表加载中 */
  const loading = ref(false)
  /** 写操作（改角色 / 删除）进行中 */
  const submitting = ref(false)
  /** 已归一化的错误文案（null 表示无错误） */
  const error = ref<string | null>(null)

  /** 总页数（至少 1 页，便于空列表展示） */
  const totalPages = computed(() =>
    Math.max(1, Math.ceil(total.value / pageSize.value)),
  )

  /** 当前页是否为空（加载结束后，用于空状态展示） */
  const isEmpty = computed(() => !loading.value && users.value.length === 0)

  /**
   * 加载指定页的用户列表（失败时写入 error，不抛出）。
   *
   * Args:
   *   targetPage: 目标页码（默认当前页）；后端会校验范围。
   */
  async function load(targetPage: number = page.value): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const result = await fetchAdminUsers(targetPage, pageSize.value)
      users.value = result.items
      total.value = result.total
      page.value = result.page
      pageSize.value = result.page_size
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      loading.value = false
    }
  }

  /** 上一页（第一页时不做任何请求） */
  async function loadPrevPage(): Promise<void> {
    if (page.value <= 1) {
      return
    }
    await load(page.value - 1)
  }

  /** 下一页（最后一页时不做任何请求） */
  async function loadNextPage(): Promise<void> {
    if (page.value >= totalPages.value) {
      return
    }
    await load(page.value + 1)
  }

  /**
   * 修改用户角色（``user`` ⇄ ``admin``）。
   *
   * 后端规则：管理员不能修改自己的角色（403）—— 前端仅禁用按钮，
   * 真正拦截在后端；这里把后端返回的错误文案原样展示。
   *
   * Returns:
   *   ``true`` 表示成功（列表已刷新）；``false`` 表示失败（error 已写入）。
   */
  async function changeRole(user: User, role: UserRole): Promise<boolean> {
    if (submitting.value) {
      return false
    }
    submitting.value = true
    error.value = null
    try {
      await updateAdminUserRole(user.id, role)
      // 写操作成功 → 重新拉取当前页，保证与服务器一致
      await load(page.value)
      return true
    } catch (err) {
      const message = errorMessage(err)
      // 失败 → 同样拉取一次，让列表（含角色下拉框）回到服务器真实状态，
      // 避免界面显示「已切换」的假象；随后写回错误文案
      // （load() 会清空 error，因此必须在它之后赋值）
      await load(page.value)
      error.value = message
      return false
    } finally {
      submitting.value = false
    }
  }

  /**
   * 删除用户（调用方必须先做二次确认）。
   *
   * 后端规则：管理员不能删除自己（403）、用户不存在（404）。
   * 若删除的是当前页最后一条且不在第一页，则自动回退一页，避免空页。
   *
   * Returns:
   *   ``true`` 表示成功；``false`` 表示失败（error 已写入）。
   */
  async function remove(user: User): Promise<boolean> {
    if (submitting.value) {
      return false
    }
    submitting.value = true
    error.value = null
    try {
      await deleteAdminUser(user.id)
      const isLastItemOnPage = users.value.length === 1 && page.value > 1
      await load(isLastItemOnPage ? page.value - 1 : page.value)
      return true
    } catch (err) {
      const message = errorMessage(err)
      // 失败（例如后端 403 禁止删除自己）→ 回拉当前页并展示真实错误，
      // 不伪造「删除成功」
      await load(page.value)
      error.value = message
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 清空错误提示 */
  function clearError(): void {
    error.value = null
  }

  /** 清空全部状态（登出时调用） */
  function reset(): void {
    users.value = []
    total.value = 0
    page.value = 1
    pageSize.value = DEFAULT_PAGE_SIZE
    loading.value = false
    submitting.value = false
    error.value = null
  }

  return {
    users,
    total,
    page,
    pageSize,
    totalPages,
    loading,
    submitting,
    error,
    isEmpty,
    load,
    loadPrevPage,
    loadNextPage,
    changeRole,
    remove,
    clearError,
    reset,
  }
})
