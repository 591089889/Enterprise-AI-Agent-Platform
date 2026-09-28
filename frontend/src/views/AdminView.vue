<script setup lang="ts">
/**
 * 系统管理 —— 平台概览 Dashboard（Stage 18）。
 *
 * 位置
 * ----
 * 平台外壳（``layouts/MainLayout.vue``）内的 ``/app/admin`` 模块页面。
 * **仅管理员可进入**：路由 ``meta.requiresAdmin``（前端体验层）
 * + 后端 ``require_admin``（真实权限边界，普通用户 403）。
 *
 * 职责
 * ----
 * 让管理员一眼看到平台当前状态：
 *
 *     用户 / Agent / 知识库 / 文档 / 会话 / 消息   ← 真实数据库 COUNT(*)
 *
 * 明确不做（本阶段边界）
 * ----------------------
 * - 不做复杂 BI：无图表库、无折线图 / 饼图、无实时监控、无日志中心；
 * - 不引入任何新 npm 依赖（纯 CSS + 现有组件风格）；
 * - **不显示任何无法可靠获取的指标**（不伪造 CPU / QPS / 健康检查结果）：
 *   「后端连接」只由「统计接口成功返回」这一事实得出。
 *
 * 数据流
 * ------
 *     本页面 → stores/dashboard.ts → api/admin.ts（GET /admin/dashboard）
 *            → AdminService.get_dashboard_stats() → Repository → PostgreSQL
 */

import { computed, onMounted } from 'vue'

import type { DashboardStats, UserRole } from '../api/admin'
import type { User } from '../api/user'
import { useAdminStore } from '../stores/admin'
import { useDashboardStore } from '../stores/dashboard'
import { useUserStore } from '../stores/user'

const dashboard = useDashboardStore()
const admin = useAdminStore()
const user = useUserStore()

/** 概览卡片定义（key 与后端 DashboardStats 字段一一对应） */
interface StatCard {
  key: keyof DashboardStats
  label: string
  hint: string
}

const STAT_CARDS: StatCard[] = [
  { key: 'users', label: '用户', hint: '平台账号总数' },
  { key: 'agents', label: 'Agent', hint: '已配置的 Agent' },
  { key: 'knowledge_bases', label: '知识库', hint: '企业知识库' },
  { key: 'documents', label: '文档', hint: '知识库中的文档' },
  { key: 'conversations', label: '会话', hint: '用户会话总数' },
  { key: 'messages', label: '消息', hint: '已落库消息总数' },
]

/** 统计快照（模板中做空值安全读取） */
const stats = computed(() => dashboard.stats)

/** 当前用户角色标签（来自真实用户资料，非硬编码） */
const roleLabel = computed(() =>
  user.user?.role === 'admin' ? '管理员' : (user.user?.role ?? '未知'),
)

/** 当前登录用户 ID（用于标记「我」这一行） */
const currentUserId = computed(() => user.user?.id ?? null)

/**
 * 是否为当前登录用户本人。
 *
 * 用途：仅用于**体验层**禁用按钮 / 提示（后端独立强制：
 * 管理员不能修改自己的角色、不能删除自己，均返回 403）。
 */
function isSelf(item: User): boolean {
  return currentUserId.value !== null && item.id === currentUserId.value
}

/** 角色列提示（只有「我」这一行需要说明为何不可改） */
function roleHint(item: User): string {
  return isSelf(item) ? '不能修改自己的角色（后端强制）' : ''
}

/** 时间格式化（列表展示） */
function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * 修改角色（下拉变更）。
 *
 * 只负责发起请求；能否修改由后端判定（403 时展示后端消息，
 * 并由 store 回拉列表，使下拉框回到服务器真实值）。
 */
async function onRoleChange(item: User, event: Event): Promise<void> {
  const select = event.target as HTMLSelectElement
  const role = select.value as UserRole
  if (role !== 'user' && role !== 'admin') {
    return
  }
  // 与当前角色相同 / 本人 → 不发请求（本人按钮本已禁用，这里是双保险）
  if (role === item.role || isSelf(item)) {
    return
  }
  await admin.changeRole(item, role)
}

/** 删除用户（必须二次确认；失败时展示后端错误，不假装成功） */
async function onDelete(item: User): Promise<void> {
  if (isSelf(item)) {
    return
  }
  const confirmed = window.confirm(
    `确定删除用户「${item.username}」？该操作不可撤销，其会话与消息会一并删除。`,
  )
  if (!confirmed) {
    return
  }
  const ok = await admin.remove(item)
  if (ok) {
    // 用户总数发生变化 → 同步 Dashboard 统计（保持与服务器一致）
    void dashboard.load()
  }
}

onMounted(() => {
  void dashboard.load()
  void admin.load()
})
</script>

<template>
  <section class="module-page">
    <header class="module-page__header">
      <div>
        <h2 class="module-page__title">系统管理 · 平台概览</h2>
        <p class="module-page__subtitle">
          平台核心资源、系统状态与用户管理（数据来自数据库实时统计）
        </p>
      </div>
      <button
        class="btn btn--sm"
        type="button"
        :disabled="dashboard.loading"
        @click="dashboard.load()"
      >
        {{ dashboard.loading ? '刷新中…' : '刷新' }}
      </button>
    </header>

    <div class="module-page__body module-page__body--top">
      <p v-if="dashboard.error" class="panel-alert">
        <span class="alert alert--error">{{ dashboard.error }}</span>
      </p>

      <!-- 首次加载中（尚无数据；不显示任何占位假数字） -->
      <div v-if="dashboard.loading && stats === null" class="state">
        <span>加载平台概览…</span>
      </div>

      <template v-else-if="stats !== null">
        <!-- 核心资源统计（6 项，全部来自真实数据库） -->
        <div class="stat-grid">
          <article v-for="card in STAT_CARDS" :key="card.key" class="stat-card">
            <p class="stat-card__label">{{ card.label }}</p>
            <p class="stat-card__value">{{ stats[card.key] }}</p>
            <p class="stat-card__hint">{{ card.hint }}</p>
          </article>
        </div>

        <!-- 系统状态：只展示确实可得的真实信息 -->
        <div class="panel">
          <div class="panel__header">
            <span class="panel__title">系统状态</span>
          </div>
          <div class="panel__body">
            <ul class="kv-list">
              <li class="kv-list__item">
                <span class="kv-list__key">后端连接</span>
                <span class="kv-list__value">
                  正常（GET /admin/dashboard 返回 200）
                </span>
              </li>
              <li class="kv-list__item">
                <span class="kv-list__key">当前用户</span>
                <span class="kv-list__value">
                  {{ user.displayName }}（{{ roleLabel }}）
                </span>
              </li>
              <li class="kv-list__item">
                <span class="kv-list__key">权限校验</span>
                <span class="kv-list__value">
                  已通过后端 require_admin（管理员专属接口）
                </span>
              </li>
              <li class="kv-list__item">
                <span class="kv-list__key">统计口径</span>
                <span class="kv-list__value">
                  数据库实时 COUNT(*)，无缓存、不估算
                </span>
              </li>
            </ul>
          </div>
        </div>

        <!-- 管理入口（跳转到已有管理模块，不在本页重复其功能） -->
        <div class="panel">
          <div class="panel__header">
            <span class="panel__title">管理入口</span>
          </div>
          <div class="panel__body">
            <div class="action-list">
              <RouterLink class="btn btn--sm" :to="{ name: 'agents' }">
                Agent 管理
              </RouterLink>
              <RouterLink class="btn btn--sm" :to="{ name: 'knowledge' }">
                知识库管理
              </RouterLink>
              <RouterLink class="btn btn--sm" :to="{ name: 'chat' }">
                AI 工作台
              </RouterLink>
            </div>
          </div>
        </div>
      </template>

      <!-- ============================================================
           用户管理（Stage 19）：管理员专属
           - 按钮禁用只是体验层；真正边界是后端 require_admin（401/403）
           - 数据来自 GET /admin/users（后端分页），不写死、不本地伪造
           ============================================================ -->
      <div class="panel">
        <div class="panel__header">
          <span class="panel__title">用户管理</span>
          <button
            class="btn btn--ghost btn--sm"
            type="button"
            :disabled="admin.loading || admin.submitting"
            @click="admin.load()"
          >
            {{ admin.loading ? '加载中…' : '刷新' }}
          </button>
        </div>

        <div class="panel__body">
          <p v-if="admin.error" class="panel-alert">
            <span class="alert alert--error">{{ admin.error }}</span>
          </p>

          <div v-if="admin.loading && admin.users.length === 0" class="state">
            <span>加载用户列表…</span>
          </div>

          <div v-else-if="admin.isEmpty" class="state">
            <span class="state__title">暂无用户</span>
          </div>

          <template v-else>
            <div class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>用户名</th>
                    <th>邮箱</th>
                    <th>角色</th>
                    <th>注册时间</th>
                    <th class="table__col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in admin.users" :key="item.id">
                    <td class="table__muted">{{ item.id }}</td>
                    <td>
                      <span class="table__strong">{{ item.username }}</span>
                      <span v-if="isSelf(item)" class="badge">我</span>
                    </td>
                    <td class="table__muted">{{ item.email }}</td>
                    <td>
                      <select
                        class="input input--inline"
                        :value="item.role"
                        :disabled="admin.submitting || isSelf(item)"
                        :title="roleHint(item)"
                        @change="onRoleChange(item, $event)"
                      >
                        <option value="user">user</option>
                        <option value="admin">admin</option>
                      </select>
                      <span v-if="roleHint(item)" class="field__hint">
                        {{ roleHint(item) }}
                      </span>
                    </td>
                    <td class="table__muted">
                      {{ formatTime(item.created_at) }}
                    </td>
                    <td class="table__actions">
                      <button
                        class="btn btn--danger btn--sm"
                        type="button"
                        :disabled="admin.submitting || isSelf(item)"
                        :title="roleHint(item)"
                        @click="onDelete(item)"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- 分页（沿用后端分页，不做客户端全量拉取） -->
            <div class="pager">
              <span class="pager__info">
                共 {{ admin.total }} 个用户 · 第 {{ admin.page }} /
                {{ admin.totalPages }} 页
              </span>
              <div class="pager__actions">
                <button
                  class="btn btn--sm"
                  type="button"
                  :disabled="admin.loading || admin.page <= 1"
                  @click="admin.loadPrevPage()"
                >
                  上一页
                </button>
                <button
                  class="btn btn--sm"
                  type="button"
                  :disabled="admin.loading || admin.page >= admin.totalPages"
                  @click="admin.loadNextPage()"
                >
                  下一页
                </button>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>
