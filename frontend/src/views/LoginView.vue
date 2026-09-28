<script setup lang="ts">
/**
 * 登录页（同时提供最小注册入口）。
 *
 * 流程
 * ----
 *     提交表单
 *       ↓  POST /auth/login
 *     保存 access_token（auth store + localStorage）
 *       ↓  GET /users/me
 *     跳转到守卫记录的来源页面（默认 /chat）
 *
 * 说明：注册直接复用后端已有的公开接口 ``POST /users/``
 * （见 backend/app/api/users.py），前端不做额外用户管理能力。
 */

import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { register } from '../api/user'
import { useAuthStore } from '../stores/auth'
import { useUserStore } from '../stores/user'
import { errorMessage } from '../utils/error'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const user = useUserStore()

/** 当前模式：登录 / 注册 */
const mode = ref<'login' | 'register'>('login')

const email = ref('')
const password = ref('')
const username = ref('')

/** 请求进行中（按钮 loading / disabled） */
const submitting = ref(false)
/** 错误文案（已归一化，直接展示） */
const error = ref<string | null>(null)
/** 注册成功提示 */
const notice = ref<string | null>(null)

const isRegister = computed(() => mode.value === 'register')

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (email.value.trim() === '' || password.value === '') return false
  if (isRegister.value && username.value.trim() === '') return false
  return true
})

/** 登录成功后要跳转的地址（守卫写入的 redirect，仅接受站内路径） */
const redirectTarget = computed(() => {
  const redirect = route.query.redirect
  if (typeof redirect === 'string' && redirect.startsWith('/')) {
    return redirect
  }
  return '/chat'
})

/** 切换 登录 / 注册 模式 */
function switchMode(): void {
  mode.value = isRegister.value ? 'login' : 'register'
  error.value = null
  notice.value = null
}

async function onSubmit(): Promise<void> {
  if (!canSubmit.value) {
    return
  }
  error.value = null
  notice.value = null
  submitting.value = true
  try {
    if (isRegister.value) {
      // 注册成功不自动登录（后端注册接口不返回 token），回到登录模式
      await register({
        username: username.value.trim(),
        email: email.value.trim(),
        password: password.value,
      })
      notice.value = '注册成功，请使用该账号登录'
      mode.value = 'login'
      password.value = ''
      return
    }

    await auth.login(email.value.trim(), password.value)
    await user.load()
    await router.replace(redirectTarget.value)
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="onSubmit">
      <h1 class="login-card__title">Enterprise AI Agent Platform</h1>
      <p class="login-card__hint">
        {{ isRegister ? '创建一个账号以开始使用 Agent' : '登录后开始与 AI Agent 对话' }}
      </p>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <label v-if="isRegister" class="field">
        <span class="field__label">用户名</span>
        <input
          v-model="username"
          class="input"
          type="text"
          maxlength="50"
          autocomplete="username"
          placeholder="alice"
        />
      </label>

      <label class="field">
        <span class="field__label">邮箱</span>
        <input
          v-model="email"
          class="input"
          type="email"
          maxlength="100"
          autocomplete="email"
          placeholder="alice@example.com"
        />
      </label>

      <label class="field">
        <span class="field__label">密码</span>
        <input
          v-model="password"
          class="input"
          type="password"
          maxlength="128"
          :autocomplete="isRegister ? 'new-password' : 'current-password'"
          placeholder="至少 6 位"
        />
      </label>

      <button class="btn btn--primary btn--block" type="submit" :disabled="!canSubmit">
        {{ submitting ? '处理中…' : isRegister ? '注册' : '登录' }}
      </button>

      <button class="login-card__switch" type="button" @click="switchMode">
        {{ isRegister ? '已有账号？返回登录' : '没有账号？立即注册' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 2rem 1rem;
}

.login-card {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  width: 100%;
  max-width: 380px;
  padding: 2rem;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-background-soft);
}

.login-card__title {
  font-size: 1.15rem;
  font-weight: 600;
  color: var(--color-heading);
}

.login-card__hint {
  font-size: 0.85rem;
  color: var(--color-text-muted);
}

.login-card__switch {
  border: none;
  background: none;
  color: var(--color-accent);
  font-size: 0.85rem;
  cursor: pointer;
  padding: 0;
}

.login-card__switch:hover {
  text-decoration: underline;
}
</style>
