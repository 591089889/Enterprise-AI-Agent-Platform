<script setup lang="ts">
/**
 * 消息列表：渲染当前会话的历史消息与 Agent 回复。
 *
 * 状态覆盖
 * --------
 * - loading：历史消息加载中
 * - empty  ：未选中会话 / 会话内暂无消息
 * - error  ：加载或发送失败（显示后端统一错误的 message，可重试）
 * - success：消息气泡
 *
 * 说明：本阶段不实现 Streaming —— 发送期间显示「AI 正在思考」占位，
 * 收到完整 reply 后一次性追加到列表。
 */

import { computed, nextTick, ref, watch } from 'vue'

import { useChatStore } from '../stores/chat'

const chat = useChatStore()

/** 滚动容器（新消息到达后自动滚到底部） */
const scroller = ref<HTMLElement | null>(null)

/** 已渲染的消息数（含发送中的占位），用于触发滚动 */
const renderedCount = computed(
  () => chat.messages.length + (chat.sending ? 1 : 0),
)

watch(renderedCount, async () => {
  await nextTick()
  const element = scroller.value
  if (element) {
    element.scrollTop = element.scrollHeight
  }
})

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** 重新加载当前会话的历史消息 */
function reload(): void {
  const conversationId = chat.conversationId
  if (conversationId !== null) {
    void chat.loadHistory(conversationId)
  }
}

/** 关闭错误提示 */
function dismissError(): void {
  chat.clearError()
}
</script>

<template>
  <div ref="scroller" class="message-list">
    <div v-if="chat.error" class="message-list__error">
      <p class="alert alert--error">{{ chat.error }}</p>
      <div class="message-list__error-actions">
        <button
          v-if="chat.conversationId !== null"
          class="btn btn--sm"
          type="button"
          @click="reload"
        >
          重新加载会话
        </button>
        <button class="btn btn--sm" type="button" @click="dismissError">
          知道了
        </button>
      </div>
    </div>

    <div v-if="chat.loading" class="state">
      <span>加载历史消息…</span>
    </div>

    <div v-else-if="chat.conversationId === null" class="state">
      <span class="state__title">开始新的对话</span>
      <span>直接输入消息并发送即可开始（会自动创建会话），也可以从左侧选择已有会话</span>
    </div>

    <div v-else-if="chat.messages.length === 0" class="state">
      <span class="state__title">开始你的第一次对话</span>
      <span>Agent 会自动决定是否需要检索知识库或调用工具</span>
    </div>

    <template v-else>
      <div
        v-for="message in chat.messages"
        :key="message.id"
        class="message"
        :class="`message--${message.role}`"
      >
        <div class="message__bubble">
          <p class="message__text">{{ message.content }}</p>
        </div>
        <span class="message__meta">
          {{ message.role === 'user' ? '我' : 'AI' }} ·
          {{ formatTime(message.created_at) }}
        </span>
      </div>
    </template>

    <div v-if="chat.sending" class="message message--assistant">
      <div class="message__bubble message__bubble--pending">
        <span>AI 正在思考…</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.message-list {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.message-list__error {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.message-list__error-actions {
  display: flex;
  gap: 0.4rem;
}

.message {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  max-width: 78%;
}

.message--user {
  align-self: flex-end;
  align-items: flex-end;
}

.message--assistant {
  align-self: flex-start;
  align-items: flex-start;
}

.message__bubble {
  border-radius: 12px;
  padding: 0.55rem 0.8rem;
  background: var(--color-surface-mute);
}

.message--user .message__bubble {
  background: var(--color-accent);
  color: #fff;
}

.message__bubble--pending {
  color: var(--color-text-muted);
  font-style: italic;
}

.message__text {
  margin: 0;
  font-size: 0.9rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.message__meta {
  font-size: 0.7rem;
  color: var(--color-text-muted);
}
</style>


