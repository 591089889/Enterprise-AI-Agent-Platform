<script setup lang="ts">
/**
 * AI 工作台页面：左侧会话列表 + 右侧 Agent Chat。
 *
 * 页面位置
 * --------
 * 本页面是平台外壳（``layouts/MainLayout.vue``）内的一个模块页面，
 * 挂在 ``/app/chat`` 下；顶栏与平台模块导航由 MainLayout / Sidebar 提供：
 *
 *     MainLayout
 *       ├── Sidebar（平台模块导航）
 *       └── router-view → ChatView
 *                           ├── ConversationList（会话列表）
 *                           └── chat-layout__panel（MessageList + MessageInput）
 *
 * 会话创建时机（重要）
 * --------------------
 * 本页面**不会自动创建会话**：组件 mounted、路由进入、页面刷新都只做只读操作
 * （加载会话列表、加载历史消息），也不会创建「临时空会话」。
 * 没有任何会话时输入框依然可用，直到用户点击发送时才由
 * ``stores/chat.ts`` 的 ``send()`` 先创建会话、再发送第一条消息。
 *
 * 状态与路由的关系
 * ----------------
 * 路由参数 ``/app/chat/:conversationId?`` 是「当前打开哪个会话」的唯一真源：
 *
 *     点击会话 → router.push(/app/chat/12) → 监听 → 同步 store → 加载历史消息
 *       ↓
 *     刷新页面 / 直接访问链接 → 路由参数仍在 → 自动恢复该会话与历史消息
 *
 * 选择这种设计的原因：把"当前会话"放在 URL 上，刷新与分享链接的行为
 * 都是一致的，不需要额外的本地持久化。
 *
 * Agent 选择（Stage 17）
 * ---------------------
 * 顶栏提供 Agent 选择器（数据来自 ``GET /agents/available``，任意登录用户可读）：
 *
 *     用户选择 Agent → chat.setAgent(id) → 发送时携带 agent_id
 *       ↓
 *     后端按该 Agent 的 provider / model / temperature / system_prompt /
 *     知识库 / 工具绑定执行（Chat 页不提供任何 Agent 编辑能力，那是 Admin）
 *
 * 默认行为：进入页面时自动选中第一个可用 Agent；没有可用 Agent 时
 * 显示「当前暂无可用 Agent」并退回平台默认配置（与 Stage 17 之前一致）。
 */

import { onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ConversationList from '../components/ConversationList.vue'
import MessageInput from '../components/MessageInput.vue'
import MessageList from '../components/MessageList.vue'
import { useAgentStore } from '../stores/agent'
import { useChatStore } from '../stores/chat'
import { useConversationStore } from '../stores/conversation'

const route = useRoute()
const router = useRouter()
const conversations = useConversationStore()
const agents = useAgentStore()
const chat = useChatStore()

/** 解析路由中的会话 ID（非法值一律视为"未选中"） */
function routeConversationId(): number | null {
  const raw = route.params.conversationId
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || value === '') {
    return null
  }
  const parsed = Number.parseInt(value, 10)
  return Number.isNaN(parsed) ? null : parsed
}

onMounted(async () => {
  // 当前用户信息（顶栏展示）由平台外壳 MainLayout 负责，这里不再重复请求。
  await conversations.load()
  // 可用 Agent 列表（选择器数据源；普通用户可读）
  await agents.loadAvailable()

  // 已指定会话（/app/chat/{id}）→ 交给下面的 watch 加载历史消息，这里不做任何跳转
  if (routeConversationId() !== null) {
    return
  }

  // 未指定会话：打开最近一个已有会话（列表按 created_at DESC 排序）。
  //
  // 注意：这里只「打开已有会话」，绝不创建会话：
  // - 账号下没有任何会话时什么都不做，输入框保持可用，
  //   用户点击发送时才创建第一条会话（见 stores/chat.ts 的 send()）；
  // - 页面刷新 / 路由进入都不会产生空会话。
  const latest = conversations.items[0]
  if (latest) {
    await openConversation(latest.id)
  }
})

/** 打开指定会话（写路由，历史消息由下面的 watch 统一加载） */
async function openConversation(conversationId: number): Promise<void> {
  if (routeConversationId() === conversationId) {
    await chat.loadHistory(conversationId)
    return
  }
  await router.push({
    name: 'chat',
    params: { conversationId: String(conversationId) },
  })
}

// 路由参数变化（含首次进入）→ 同步选中状态 + 加载历史消息
watch(
  routeConversationId,
  (conversationId) => {
    conversations.select(conversationId)
    if (conversationId === null) {
      chat.reset()
      return
    }
    void chat.loadHistory(conversationId)
  },
  { immediate: true },
)

// 首次发送时由 chat store 创建会话（此时 URL 还是 /app/chat，没有 conversationId）：
// 会话创建成功后把 URL 同步为 /app/chat/{id}，使刷新 / 分享链接与
// 「手动选择会话」的行为完全一致（随后上面的 watch 会按路由重新加载历史消息）。
// 使用 replace 而不是 push：避免返回键回到「没有会话」的中间态。
watch(
  () => chat.conversationId,
  (conversationId) => {
    if (conversationId !== null && routeConversationId() !== conversationId) {
      void router.replace({
        name: 'chat',
        params: { conversationId: String(conversationId) },
      })
    }
  },
)

/**
 * Agent 选择（Stage 17）
 *
 * 保持「选中态」始终合法：
 * - 没有可用 Agent → 清空选择（发送时不带 agent_id，沿用平台默认配置）；
 * - 已选 Agent 被管理员删除 / 首次进入 → 自动回退到第一个可用 Agent。
 */
watch(
  () => agents.available,
  (list) => {
    const [first] = list
    if (first === undefined) {
      // 没有任何可用 Agent → 清空选择（发送时不带 agent_id）
      chat.setAgent(null)
      return
    }
    if (!list.some((item) => item.id === chat.agentId)) {
      chat.setAgent(first.id)
    }
  },
  { immediate: true },
)

/** 选择器变更 → 写回 chat store（发送时由 store 携带 agent_id） */
function selectAgent(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  chat.setAgent(value === '' ? null : Number.parseInt(value, 10))
}
</script>

<template>
  <div class="workspace">
    <header class="workspace__header">
      <div class="workspace__heading">
        <h2 class="workspace__title">AI 工作台</h2>
        <p class="workspace__subtitle">与 AI Agent 对话</p>
      </div>

      <!-- Agent 选择器（Stage 17）：决定本次对话使用哪套 Agent 配置 -->
      <label class="workspace__agent">
        <span class="workspace__agent-label">Agent</span>
        <select
          class="workspace__agent-select"
          :value="chat.agentId === null ? '' : String(chat.agentId)"
          :disabled="chat.sending || !agents.hasAvailable"
          @change="selectAgent"
        >
          <option v-if="!agents.hasAvailable" value="">
            当前暂无可用 Agent
          </option>
          <option
            v-for="agent in agents.available"
            :key="agent.id"
            :value="String(agent.id)"
          >
            {{ agent.name }}
          </option>
        </select>
      </label>
    </header>
    <p v-if="agents.availableError" class="workspace__agent-error">
      {{ agents.availableError }}
    </p>

    <div class="chat-layout">
      <ConversationList />
      <section class="chat-layout__panel">
        <div class="chat-layout__body">
          <MessageList />
        </div>
        <MessageInput />
      </section>
    </div>
  </div>
</template>
