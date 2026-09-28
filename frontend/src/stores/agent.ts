/**
 * Agent Store（Stage 15 / Stage 17）：Agent 列表状态与 CRUD 动作。
 *
 * 两类状态（同一 Agent 领域）
 * --------------------------
 * 1. **管理态**（``items`` + ``load/create/update/remove``）：
 *    仅管理员可见的 Agent 配置管理（页面：AgentManagementView）；
 * 2. **可用态**（``available`` + ``loadAvailable``，Stage 17）：
 *    任意登录用户可读的最小字段列表（id / name / description），
 *    供 AI 工作台的 Agent 选择器使用（页面：ChatView）。
 *
 * 设计原则（与 stores/conversation.ts 保持一致）
 * ---------------------------------------------
 * - 页面只读 store，不直接调用 ``api/*``；
 * - 错误在 store 内归一化为「可直接展示的文案」（``utils/error.ts``）；
 * - ``loading``    列表加载中；``submitting`` 新建/修改/删除进行中（按钮禁用）；
 * - 写操作成功后统一 ``load()`` 刷新列表，避免前端与后端不一致。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  createAgent as createAgentApi,
  deleteAgent as deleteAgentApi,
  getAgent as getAgentApi,
  listAgentTools,
  listAgents,
  listAvailableAgents,
  updateAgent as updateAgentApi,
  type AgentAvailable,
  type AgentConfig,
  type AgentCreatePayload,
  type AgentToolOption,
  type AgentUpdatePayload,
} from '../api/agent'
import { errorMessage } from '../utils/error'

export const useAgentStore = defineStore('agent', () => {
  const items = ref<AgentConfig[]>([])
  const loading = ref(false)
  const submitting = ref(false)
  const error = ref<string | null>(null)

  /** 可用 Agent 列表（Chat 页面选择器；最小字段集） */
  const available = ref<AgentAvailable[]>([])
  const availableLoading = ref(false)
  const availableError = ref<string | null>(null)

  /** 可绑定工具列表（管理页面工具多选项；来自后端 ToolRegistry） */
  const toolOptions = ref<AgentToolOption[]>([])
  const toolOptionsLoading = ref(false)

  /** 是否为空列表（加载结束后无数据，用于空状态展示） */
  const isEmpty = computed(() => !loading.value && items.value.length === 0)

  /** 是否没有可用 Agent（Chat 页面显示「当前暂无可用 Agent」） */
  const hasAvailable = computed(() => available.value.length > 0)

  /** 加载 Agent 列表（失败时写入 error，不抛出） */
  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      items.value = await listAgents()
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      loading.value = false
    }
  }

  /**
   * 加载「可用 Agent」列表（Chat 页面使用，任意登录用户可调用）。
   *
   * 与 ``load()`` 相互独立：失败只写入 ``availableError``，
   * 不影响管理页面的错误提示。
   */
  async function loadAvailable(): Promise<void> {
    availableLoading.value = true
    availableError.value = null
    try {
      available.value = await listAvailableAgents()
    } catch (err) {
      availableError.value = errorMessage(err)
    } finally {
      availableLoading.value = false
    }
  }

  /**
   * 加载可绑定工具列表（管理页面使用，仅管理员可调用）。
   *
   * 失败时写入 ``error``（与列表加载共用同一错误提示位）。
   */
  async function loadToolOptions(): Promise<void> {
    toolOptionsLoading.value = true
    try {
      toolOptions.value = await listAgentTools()
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      toolOptionsLoading.value = false
    }
  }

  /** 按 ID 读取最新配置（编辑前调用，避免使用列表中的陈旧数据） */
  async function fetchOne(agentId: number): Promise<AgentConfig | null> {
    error.value = null
    try {
      return await getAgentApi(agentId)
    } catch (err) {
      error.value = errorMessage(err)
      return null
    }
  }

  /** 创建 Agent（成功返回新对象并刷新列表；失败返回 null） */
  async function create(
    payload: AgentCreatePayload,
  ): Promise<AgentConfig | null> {
    submitting.value = true
    error.value = null
    try {
      const created = await createAgentApi(payload)
      await load()
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 更新 Agent（成功返回更新后的对象并刷新列表；失败返回 null） */
  async function update(
    agentId: number,
    payload: AgentUpdatePayload,
  ): Promise<AgentConfig | null> {
    submitting.value = true
    error.value = null
    try {
      const updated = await updateAgentApi(agentId, payload)
      await load()
      return updated
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 删除 Agent（成功后就地移除并刷新列表；失败返回 false） */
  async function remove(agentId: number): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      await deleteAgentApi(agentId)
      items.value = items.value.filter((item) => item.id !== agentId)
      await load()
      return true
    } catch (err) {
      error.value = errorMessage(err)
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
    items.value = []
    loading.value = false
    submitting.value = false
    error.value = null
    available.value = []
    availableLoading.value = false
    availableError.value = null
    toolOptions.value = []
    toolOptionsLoading.value = false
  }

  return {
    items,
    loading,
    submitting,
    error,
    available,
    availableLoading,
    availableError,
    toolOptions,
    toolOptionsLoading,
    isEmpty,
    hasAvailable,
    load,
    loadAvailable,
    loadToolOptions,
    fetchOne,
    create,
    update,
    remove,
    clearError,
    reset,
  }
})