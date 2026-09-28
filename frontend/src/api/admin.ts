/**
 * Admin API：管理员专属接口（``/admin/*``）。
 *
 * 权限
 * ----
 * 这些接口在后端全部由 ``require_admin`` 强制校验：
 * 未登录 401、普通用户 403。前端路由守卫 / 菜单隐藏只是体验层，
 * **不能替代**后端的真实权限边界。
 *
 * 请求统一走 ``utils/request.ts``（自动附加 Bearer + 错误归一化为 ApiError）。
 *
 * 覆盖
 * ----
 * - 平台概览统计（Stage 18）：``fetchDashboardStats``
 * - 用户管理（Stage 19）：``fetchAdminUsers`` / ``updateAdminUserRole`` /
 *   ``deleteAdminUser``
 */

import request from '../utils/request'
import type { User } from './user'

/** 平台概览统计（对齐 backend/app/schemas/admin.py: DashboardStats） */
export interface DashboardStats {
  users: number
  agents: number
  knowledge_bases: number
  documents: number
  conversations: number
  messages: number
}

/**
 * 管理员用户列表（对齐 backend/app/schemas/admin.py: PaginatedUsersResponse）。
 *
 * ``items`` 的元素与 ``UserRead`` 完全一致，因此复用 ``api/user.ts`` 的 ``User``。
 */
export interface PaginatedUsers {
  items: User[]
  /** 用户总数（不受分页影响） */
  total: number
  /** 当前页码（从 1 开始） */
  page: number
  /** 每页数量（后端限制 1–100） */
  page_size: number
}

/** 角色取值（对齐后端 ``Literal["user", "admin"]``） */
export type UserRole = 'user' | 'admin'

/**
 * 获取平台概览统计（Dashboard 数据源）。
 *
 * 后端返回的是数据库实时 ``COUNT(*)``（不缓存、不估算），
 * 因此页面上的数字与数据库始终一致。
 *
 * 可能的错误（由后端统一错误结构返回，前端只展示 message）：
 * - 401 UNAUTHORIZED          未登录 / Token 失效
 * - 403 PERMISSION_DENIED     非管理员
 */
export async function fetchDashboardStats(): Promise<DashboardStats> {
  const { data } = await request.get<DashboardStats>('/admin/dashboard')
  return data
}

/**
 * 获取用户列表（分页）。
 *
 * 分页由后端提供（``page`` / ``page_size``），前端只按页请求，
 * 不在客户端做二次分页或缓存。
 *
 * 可能的错误：
 * - 401 未登录 / Token 失效（request.ts 统一处理）
 * - 403 非管理员
 */
export async function fetchAdminUsers(
  page = 1,
  pageSize = 20,
): Promise<PaginatedUsers> {
  const { data } = await request.get<PaginatedUsers>('/admin/users', {
    params: { page, page_size: pageSize },
  })
  return data
}

/**
 * 修改指定用户的角色。
 *
 * 业务规则由后端强制（前端只负责交互）：
 * - 角色只允许 ``user`` / ``admin``（非法值 → 422）；
 * - **管理员不能修改自己的角色**（后端 403）；
 * - 目标用户不存在 → 404。
 */
export async function updateAdminUserRole(
  userId: number,
  role: UserRole,
): Promise<User> {
  const { data } = await request.patch<User>(
    `/admin/users/${userId}/role`,
    { role },
  )
  return data
}

/**
 * 删除指定用户（级联删除其会话与消息，由数据库外键保证）。
 *
 * 业务规则由后端强制：**管理员不能删除自己**（403）；用户不存在 → 404。
 * 后端返回 204（无响应体）。
 */
export async function deleteAdminUser(userId: number): Promise<void> {
  await request.delete(`/admin/users/${userId}`)
}
