<script setup lang="ts">
/**
 * 平台模块导航（侧边栏）。
 *
 * 职责边界
 * --------
 * 只做「模块级跳转 + 当前模块高亮 + 按角色显隐」：
 *
 * - 数据来自本地常量 ``NAV_ITEMS``（平台模块是固定的产品结构，不来自后端）；
 * - 只读取 ``stores/user.ts`` 的 ``isAdmin`` 决定管理类模块是否可见
 *   （体验层控制；真正的权限由后端 ``require_admin`` 强制）；
 * - 不承载任何业务逻辑：会话列表（``ConversationList``）属于 AI 工作台页面内部。
 *
 * 图标说明：暂用单色 Unicode 字符，避免为此引入图标库。
 */

import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { useUserStore } from '../stores/user'

/** 平台模块导航项（``name`` 为路由名，见 ``router/index.ts``） */
interface NavItem {
  name: string
  label: string
  icon: string
  /** true 表示仅管理员可见（与路由 meta.requiresAdmin 对应） */
  requiresAdmin?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { name: 'chat', label: 'AI 工作台', icon: '◈' },
  { name: 'agents', label: 'Agent 管理', icon: '⬡', requiresAdmin: true },
  { name: 'knowledge', label: '知识库', icon: '▤', requiresAdmin: true },
  // 平台概览 Dashboard：仅管理员可见（与路由 meta.requiresAdmin 对应）
  { name: 'admin', label: '系统管理', icon: '▦', requiresAdmin: true },
]

const route = useRoute()
const user = useUserStore()

/** 当前用户可见的导航项（普通用户看不到管理类模块） */
const visibleItems = computed(() =>
  NAV_ITEMS.filter((item) => !item.requiresAdmin || user.isAdmin),
)

/** 当前模块是否高亮（子路由如 ``/app/chat/{id}`` 的路由名仍是 ``chat``） */
function isActive(name: string): boolean {
  return route.name === name
}
</script>

<template>
  <aside class="sidebar">
    <p class="sidebar__heading">平台模块</p>
    <nav class="sidebar__nav">
      <RouterLink
        v-for="item in visibleItems"
        :key="item.name"
        class="sidebar__item"
        :class="{ 'sidebar__item--active': isActive(item.name) }"
        :to="{ name: item.name }"
      >
        <span class="sidebar__icon" aria-hidden="true">{{ item.icon }}</span>
        <span class="sidebar__label">{{ item.label }}</span>
      </RouterLink>
    </nav>
  </aside>
</template>
