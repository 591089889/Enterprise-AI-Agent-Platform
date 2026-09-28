/**
 * 用户 API：GET /users/me、POST /users/
 *
 * 说明：``GET /users/me`` 需要 Bearer Token（由 request.ts 自动附加）。
 */

import request from '../utils/request'

/** 用户信息（对齐 backend/app/schemas/user.py: UserRead） */
export interface User {
  id: number
  username: string
  email: string
  /** 角色：user / admin（前端据此控制管理模块的菜单与路由可见性） */
  role: string
  created_at: string
}

/** 注册请求体（对齐 backend/app/schemas/user.py: UserCreate） */
export interface RegisterRequest {
  username: string
  email: string
  password: string
}

/**
 * 获取当前登录用户信息。
 *
 * Token 无效 / 过期时后端返回 401，由 request.ts 统一处理（清 token + 回登录页）。
 */
export async function fetchCurrentUser(): Promise<User> {
  const { data } = await request.get<User>('/users/me')
  return data
}

/** 注册新用户（后端路径为 ``/users/``，需保留结尾斜杠） */
export async function register(payload: RegisterRequest): Promise<User> {
  const { data } = await request.post<User>('/users/', payload)
  return data
}
