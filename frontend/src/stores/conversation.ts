/**
 * 会话 Store：会话列表（分页）/ 新建 / 重命名 / 删除 / 当前选中项。
 *
 * 状态设计
 * --------
 * - ``loading``    列表加载中（用于「加载中」占位）；
 * - ``submitting`` 新建/重命名/删除进行中（按钮禁用，避免重复提交）；
 * - ``error``      已归一化的错误文案（显示给用户）；
 * - ``selectedId`` 当前打开的会话（唯一真源，路由据此同步）。
 *
 * 所有错误都在这里收敛为「可直接展示的文案」，页面不再碰原始异常。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  createConversation as createConversationApi,
  deleteConversation as deleteConversationApi,
  listConversations,
  renameConversation as renameConversationApi,
  type Conversation,
} from '../api/conversation'
import { errorMessage } from '../utils/error'

/** 默认每页数量（后端 page_size 上限 100） */
const DEFAULT_PAGE_SIZE = 20

export const useConversationStore = defineStore('conversation', () => {
  const items = ref<Conversation[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(DEFAULT_PAGE_SIZE)
  const selectedId = ref<number | null>(null)

  const loading = ref(false)
  const submitting = ref(false)
  const error = ref<string | null>(null)

  /** 是否还有下一页（用于「加载更多」） */
  const hasMore = computed(() => items.value.length < total.value)

  /** 是否为空列表（加载结束后无数据） */
  const isEmpty = computed(() => !loading.value && items.value.length === 0)

  /** 加载会话列表；``append=true`` 时追加下一页 */
  async function load(targetPage = 1, append = false): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const data = await listConversations({
        page: targetPage,
        page_size: pageSize.value,
      })
      items.value = append ? [...items.value, ...data.items] : data.items
      total.value = data.total
      page.value = data.page
      pageSize.value = data.page_size
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      loading.value = false
    }
  }

  /** 加载下一页并追加 */
  async function loadMore(): Promise<void> {
    if (loading.value || !hasMore.value) {
      return
    }
    await load(page.value + 1, true)
  }

  /**
   * 新建会话 → 刷新列表 → 选中新会话。
   *
   * 失败返回 null（错误文案已写入 ``error``）。
   */
  async function create(title = ''): Promise<Conversation | null> {
    submitting.value = true
    error.value = null
    try {
      const created = await createConversationApi(title)
      // 列表按 created_at DESC 排序，新会话必然出现在第一页
      await load(1)
      selectedId.value = created.id
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 重命名会话（成功后就地更新列表项，不重新请求列表） */
  async function rename(conversationId: number, title: string): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      const updated = await renameConversationApi(conversationId, title)
      items.value = items.value.map((item) =>
        item.id === updated.id ? updated : item,
      )
      return true
    } catch (err) {
      error.value = errorMessage(err)
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 删除会话（成功后就地移除，并重新拉取第一页保持分页一致） */
  async function remove(conversationId: number): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      await deleteConversationApi(conversationId)
      items.value = items.value.filter((item) => item.id !== conversationId)
      total.value = Math.max(0, total.value - 1)
      if (selectedId.value === conversationId) {
        selectedId.value = null
      }
      await load(1)
      return true
    } catch (err) {
      error.value = errorMessage(err)
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 选中会话（null 表示未选中任何会话） */
  function select(conversationId: number | null): void {
    selectedId.value = conversationId
  }

  /** 清空会话状态（登出时调用） */
  function reset(): void {
    items.value = []
    total.value = 0
    page.value = 1
    pageSize.value = DEFAULT_PAGE_SIZE
    selectedId.value = null
    loading.value = false
    submitting.value = false
    error.value = null
  }

  return {
    items,
    total,
    page,
    pageSize,
    selectedId,
    loading,
    submitting,
    error,
    hasMore,
    isEmpty,
    load,
    loadMore,
    create,
    rename,
    remove,
    select,
    reset,
  }
})
