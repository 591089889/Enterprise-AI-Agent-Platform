/**
 * Chat Store：当前会话的消息流与发送状态。
 *
 * 数据来源
 * --------
 * - 历史消息：``GET /conversations/{id}/messages``（切换会话 / 刷新页面时加载）；
 * - 新消息：``POST /agent/chat``（RAG / Tool / Provider 由后端内部决定）；
 * - 首次发送：当前没有会话时，由本 Store 先 ``POST /conversations`` 创建会话，
 *   拿到 ``conversationId`` 后再发送第一条消息（见 :func:`send`）。
 *
 * Agent 选择（Stage 17）
 * ---------------------
 * 发送时携带 ``agent_id``，后端据此使用该 Agent 配置的
 * provider / model / temperature / system_prompt / 知识库 / 工具绑定执行。
 * 选择项来自 ``GET /agents/available``（任意登录用户可读），
 * 由 ChatView 在页面加载时填充；本 Store 只保存「当前选中的 Agent ID」。
 * 未选中任何 Agent 时不携带 ``agent_id``（后端沿用平台默认配置）。
 *
 * 会话创建时机（重要）
 * --------------------
 * **只有用户点击发送**才会创建会话：
 *
 * - 页面 mounted、路由进入、刷新都不会创建任何会话；
 * - 不会创建「临时空会话」；
 * - 没有会话时输入框照常可用，用户可以直接输入并点击发送。
 *
 * 本阶段不实现 Streaming：发送期间只显示「正在思考」占位，
 * 收到完整 reply 后一次性追加到消息列表。
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'

import { sendAgentMessage } from '../api/agent'
import { listMessages, type Message } from '../api/conversation'
import { errorMessage } from '../utils/error'
import { useConversationStore } from './conversation'

/** 单条消息长度上限（与后端 AgentChatRequest.message 的 max_length 保持一致） */
export const MAX_MESSAGE_LENGTH = 4000

/** 前端渲染用的消息模型 */
export interface ChatMessage {
  /** 后端消息 ID；本地乐观插入的消息使用负数临时 ID */
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
  /** true 表示前端本地插入（尚未从后端读回真实 ID） */
  local?: boolean
}

/** 本地乐观消息的临时 ID 种子（负数，避免与后端 ID 冲突） */
let tempIdSeed = -1

/** 后端消息 → 前端消息（role 收敛为 user / assistant 两种） */
function toChatMessage(message: Message): ChatMessage {
  return {
    id: message.id,
    role: message.role === 'assistant' ? 'assistant' : 'user',
    content: message.content,
    created_at: message.created_at,
  }
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<ChatMessage[]>([])
  const conversationId = ref<number | null>(null)
  /** 当前选中的 Agent ID（Stage 17；null 表示未选择，后端用平台默认配置） */
  const agentId = ref<number | null>(null)
  /** 历史消息加载中 */
  const loading = ref(false)
  /** Agent 正在生成回复（发送按钮禁用依据） */
  const sending = ref(false)
  /** 已归一化的错误文案 */
  const error = ref<string | null>(null)

  /** 选择本次对话使用的 Agent（由 ChatView 的 Agent 选择器调用） */
  function setAgent(id: number | null): void {
    agentId.value = id
  }

  /** 加载指定会话的历史消息（切换会话 / 刷新页面） */
  async function loadHistory(id: number): Promise<void> {
    conversationId.value = id
    messages.value = []
    error.value = null
    loading.value = true
    try {
      const history = await listMessages(id)
      // 防竞态：请求期间用户可能已经切换到别的会话
      if (conversationId.value !== id) {
        return
      }
      messages.value = history.map(toChatMessage)
    } catch (err) {
      if (conversationId.value === id) {
        error.value = errorMessage(err)
      }
    } finally {
      if (conversationId.value === id) {
        loading.value = false
      }
    }
  }

  /**
   * 发送消息并等待 Agent 回复（不要求调用方先准备会话）。
   *
   * 顺序（重要）
   * ------------
   *
   *     用户点击发送（本函数唯一的调用时机）
   *       ↓  当前已有会话？ → 直接使用 conversationId
   *       ↓  当前没有会话？ → 先 POST /conversations 创建会话
   *       ↓  得到 conversationId
   *       ↓  POST /agent/chat（conversation_id + message）
   *       ↓  后端保存 user / assistant 消息并返回 reply
   *
   * 后端 ``POST /agent/chat`` 要求会话已存在（conversation_id >= 1），
   * 因此「先创建、再发送」的顺序不可颠倒。
   *
   * 返回值语义与之前一致：true 表示成功（调用方据此清空输入框）；
   * false 表示失败（已回滚乐观插入的用户消息，调用方应保留输入内容以便重试）。
   */
  async function send(text: string): Promise<boolean> {
    const content = text.trim()
    if (!content || sending.value) {
      return false
    }
    if (content.length > MAX_MESSAGE_LENGTH) {
      error.value = `消息长度不能超过 ${MAX_MESSAGE_LENGTH} 个字符`
      return false
    }

    error.value = null
    sending.value = true

    /** 本次发送的目标会话（可能在下面被创建） */
    let targetId = conversationId.value

    try {
      if (targetId === null) {
        // 没有任何会话 → 先创建（这是会话唯一的创建时机：用户显式发送）
        const conversations = useConversationStore()
        const created = await conversations.create()
        if (created === null) {
          // 创建失败：原因已在 conversationStore.error（侧栏提示）中，
          // 这里同步一份到聊天区，输入内容由调用方保留，可直接重试
          error.value = conversations.error ?? '创建会话失败，请重试'
          return false
        }
        targetId = created.id
        // 新会话不应携带上一条会话的消息（正常路径下这里本就是空列表）
        messages.value = []
      }

      // 乐观显示用户消息（后端不返回消息 ID，刷新后由历史接口回填真实 ID）
      const pending: ChatMessage = {
        id: tempIdSeed--,
        role: 'user',
        content,
        created_at: new Date().toISOString(),
        local: true,
      }
      messages.value = [...messages.value, pending]

      try {
        const { reply } = await sendAgentMessage({
          conversation_id: targetId,
          message: content,
          // 选中 Agent 时携带 agent_id（后端按该 Agent 配置执行）；
          // 未选中时不携带该字段，保持与 Stage 17 之前一致的行为
          ...(agentId.value === null ? {} : { agent_id: agentId.value }),
        })
        messages.value = [
          ...messages.value,
          {
            id: tempIdSeed--,
            role: 'assistant',
            content: reply,
            created_at: new Date().toISOString(),
            local: true,
          },
        ]
        return true
      } catch (err) {
        // 失败：移除乐观消息，避免「看起来已发送成功」的假象
        messages.value = messages.value.filter((m) => m.id !== pending.id)
        error.value = errorMessage(err)
        return false
      }
    } finally {
      // 会话一旦创建成功就对外可见（路由据此同步为 /app/chat/{id}）。
      // 即使本轮 LLM 调用失败也保留该会话：用户重试时复用同一个会话，
      // 不会因为重试而重复创建。
      if (targetId !== null) {
        conversationId.value = targetId
      }
      sending.value = false
    }
  }

  /** 清空错误提示 */
  function clearError(): void {
    error.value = null
  }

  /** 清空聊天状态（登出 / 离开会话时调用；已选 Agent 一并清空） */
  function reset(): void {
    messages.value = []
    conversationId.value = null
    agentId.value = null
    loading.value = false
    sending.value = false
    error.value = null
  }

  return {
    messages,
    conversationId,
    agentId,
    loading,
    sending,
    error,
    setAgent,
    loadHistory,
    send,
    clearError,
    reset,
  }
})
