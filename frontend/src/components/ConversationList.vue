<script setup lang="ts">
/**
 * 会话列表（左侧栏）。
 *
 * 覆盖功能
 * --------
 * - 列表展示（分页 + 加载更多）
 * - 新建会话（POST /conversations → 刷新列表 → 进入新会话）
 * - 重命名（PATCH /conversations/{id}，行内编辑）
 * - 删除（DELETE /conversations/{id}）
 * - 页面状态：loading / empty / error / success
 *
 * 选中项的高亮来自 ``conversations.selectedId``（由 ChatView 依据路由同步）。
 */

import { ref } from 'vue'
import { useRouter } from 'vue-router'

import type { Conversation } from '../api/conversation'
import { useChatStore } from '../stores/chat'
import { useConversationStore } from '../stores/conversation'

/** 后端 ConversationUpdate.title 的长度约束（前端先校验，避免无谓请求） */
const TITLE_MAX_LENGTH = 200

const router = useRouter()
const conversations = useConversationStore()
const chat = useChatStore()

/** 正在重命名的会话 ID（null 表示没有行内编辑进行中） */
const editingId = ref<number | null>(null)
const editingTitle = ref('')
const renameError = ref<string | null>(null)

/** 标题展示（后端允许空标题，空标题统一显示占位文案） */
function displayTitle(conversation: Conversation): string {
  return conversation.title.trim() === '' ? '未命名会话' : conversation.title
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** 打开会话（只改路由；历史消息由 ChatView 的路由监听统一加载） */
function openConversation(conversationId: number): void {
  if (editingId.value !== null) {
    return
  }
  void router.push({
    name: 'chat',
    params: { conversationId: String(conversationId) },
  })
}

/** 新建会话并立即进入 */
async function onCreate(): Promise<void> {
  const created = await conversations.create()
  if (created) {
    await router.push({
      name: 'chat',
      params: { conversationId: String(created.id) },
    })
  }
}

/** 进入行内重命名 */
function startRename(conversation: Conversation): void {
  editingId.value = conversation.id
  editingTitle.value = conversation.title
  renameError.value = null
}

/** 取消重命名 */
function cancelRename(): void {
  editingId.value = null
  editingTitle.value = ''
  renameError.value = null
}

/** 提交重命名 */
async function submitRename(): Promise<void> {
  const conversationId = editingId.value
  if (conversationId === null) {
    return
  }
  const title = editingTitle.value.trim()

  if (title.length === 0) {
    renameError.value = '标题不能为空'
    return
  }
  if (title.length > TITLE_MAX_LENGTH) {
    renameError.value = `标题不能超过 ${TITLE_MAX_LENGTH} 个字符`
    return
  }

  const ok = await conversations.rename(conversationId, title)
  if (ok) {
    cancelRename()
  } else {
    renameError.value = conversations.error ?? '重命名失败，请重试'
  }
}

/** 删除会话（删除当前打开的会话时回到"未选中"状态） */
async function onDelete(conversation: Conversation): Promise<void> {
  const confirmed = window.confirm(
    `确定删除会话「${displayTitle(conversation)}」吗？该会话的聊天记录会一并删除。`,
  )
  if (!confirmed) {
    return
  }

  const ok = await conversations.remove(conversation.id)
  if (ok && chat.conversationId === conversation.id) {
    await router.push({ name: 'chat' })
  }
}
</script>

<template>
  <aside class="chat-layout__sidebar">
    <div class="sidebar__header">
      <span class="sidebar__title">会话</span>
      <button
        class="btn btn--primary btn--sm"
        type="button"
        :disabled="conversations.submitting"
        @click="onCreate"
      >
        + 新建
      </button>
    </div>

    <p v-if="conversations.error" class="sidebar__alert">
      <span class="alert alert--error">{{ conversations.error }}</span>
    </p>

    <div
      v-if="conversations.loading && conversations.items.length === 0"
      class="state"
    >
      <span>加载中…</span>
    </div>

    <div v-else-if="conversations.isEmpty" class="state">
      <span class="state__title">暂无会话</span>
      <span>点击「+ 新建」开始对话</span>
    </div>

    <ul v-else class="sidebar__list">
      <li
        v-for="conversation in conversations.items"
        :key="conversation.id"
        class="conversation"
        :class="{
          'conversation--active': conversation.id === conversations.selectedId,
        }"
      >
        <template v-if="editingId === conversation.id">
          <input
            v-model="editingTitle"
            class="input input--sm"
            type="text"
            :maxlength="TITLE_MAX_LENGTH"
            @keydown.enter.prevent="submitRename"
            @keydown.esc.prevent="cancelRename"
          />
          <p v-if="renameError" class="conversation__error">{{ renameError }}</p>
          <div class="conversation__actions">
            <button class="btn btn--sm" type="button" @click="submitRename">
              保存
            </button>
            <button class="btn btn--sm" type="button" @click="cancelRename">
              取消
            </button>
          </div>
        </template>

        <template v-else>
          <button
            class="conversation__main"
            type="button"
            @click="openConversation(conversation.id)"
          >
            <span class="conversation__title">{{ displayTitle(conversation) }}</span>
            <span class="conversation__time">
              {{ formatTime(conversation.updated_at) }}
            </span>
          </button>
          <div class="conversation__actions">
            <button class="btn btn--sm" type="button" @click="startRename(conversation)">
              重命名
            </button>
            <button
              class="btn btn--danger btn--sm"
              type="button"
              :disabled="conversations.submitting"
              @click="onDelete(conversation)"
            >
              删除
            </button>
          </div>
        </template>
      </li>
    </ul>

    <div
      v-if="conversations.hasMore && !conversations.isEmpty"
      class="sidebar__footer"
    >
      <button
        class="btn btn--ghost btn--block"
        type="button"
        :disabled="conversations.loading"
        @click="conversations.loadMore()"
      >
        {{ conversations.loading ? '加载中…' : '加载更多' }}
      </button>
    </div>
  </aside>
</template>

<style scoped>
.sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 0.9rem;
  border-bottom: 1px solid var(--color-border);
}

.sidebar__title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--color-heading);
}

.sidebar__alert {
  padding: 0.5rem 0.9rem 0;
}

.sidebar__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  list-style: none;
  margin: 0;
  padding: 0.5rem;
}

.sidebar__footer {
  padding: 0.5rem;
  border-top: 1px solid var(--color-border);
}

.conversation {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.35rem;
  border-radius: 8px;
  padding: 0.4rem;
  margin-bottom: 0.25rem;
}

.conversation--active {
  background: var(--color-accent-soft);
}

.conversation__main {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.15rem;
  flex: 1;
  min-width: 0;
  border: none;
  background: none;
  padding: 0;
  cursor: pointer;
  text-align: left;
}

.conversation__title {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.85rem;
  color: var(--color-text);
}

.conversation__time {
  font-size: 0.7rem;
  color: var(--color-text-muted);
}

.conversation__actions {
  display: flex;
  gap: 0.25rem;
}

.conversation__error {
  width: 100%;
  font-size: 0.72rem;
  color: var(--color-danger);
}

.input--sm {
  font-size: 0.8rem;
  padding: 0.3rem 0.45rem;
}
</style>


