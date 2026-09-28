<script setup lang="ts">
/**
 * 消息输入区。
 *
 * 交互约定
 * --------
 * - Enter 发送 / Shift + Enter 换行（输入法组合期间的 Enter 不触发发送）；
 * - 发送中禁用输入框与按钮，避免重复提交；
 * - **没有会话时同样可以输入与发送**：会话创建发生在用户点击发送之后
 *   （``stores/chat.ts`` 的 ``send()``：先 POST /conversations 再 POST /agent/chat），
 *   因此输入框只依赖「是否正在发送」，不依赖当前是否已有会话；
 * - 发送失败：恢复输入内容，方便直接重试；错误提示由消息列表统一渲染。
 */

import { computed, ref } from 'vue'

import { MAX_MESSAGE_LENGTH, useChatStore } from '../stores/chat'

const chat = useChatStore()

/** 输入草稿 */
const draft = ref('')

/** 正在发送 / 内容为空时不能发送（与当前是否已有会话无关） */
const canSend = computed(
  () => !chat.sending && draft.value.trim().length > 0,
)

async function onSend(): Promise<void> {
  if (!canSend.value) {
    return
  }

  const text = draft.value
  draft.value = ''

  // 没有会话时由 store 先创建会话再发送（见 stores/chat.ts: send）
  const ok = await chat.send(text)
  if (!ok) {
    // 发送失败：恢复草稿，避免用户输入丢失
    draft.value = text
  }
}

/** Enter 发送；Shift + Enter 换行；输入法组合中不发送 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) {
    return
  }
  event.preventDefault()
  void onSend()
}
</script>

<template>
  <form class="composer" @submit.prevent="onSend">
    <textarea
      v-model="draft"
      class="textarea"
      rows="3"
      :maxlength="MAX_MESSAGE_LENGTH"
      :disabled="chat.sending"
      placeholder="输入消息，Enter 发送 / Shift + Enter 换行"
      @keydown="onKeydown"
    />

    <div class="composer__footer">
      <span class="composer__hint">
        <template v-if="chat.conversationId === null">
          首次发送将自动创建会话 ·
          {{ draft.length }} / {{ MAX_MESSAGE_LENGTH }}
        </template>
        <template v-else>{{ draft.length }} / {{ MAX_MESSAGE_LENGTH }}</template>
      </span>
      <button class="btn btn--primary" type="submit" :disabled="!canSend">
        {{ chat.sending ? 'AI 正在生成…' : '发送' }}
      </button>
    </div>
  </form>
</template>

<style scoped>
.composer {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.75rem 1.25rem 1rem;
  border-top: 1px solid var(--color-border);
  background: var(--color-surface-soft);
}

.composer__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.composer__hint {
  font-size: 0.75rem;
  color: var(--color-text-muted);
}
</style>


