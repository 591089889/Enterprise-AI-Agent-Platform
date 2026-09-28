/**
 * 认证 API：POST /auth/login
 *
 * 后端返回：{ access_token, token_type }
 */

import request from '../utils/request'

/** 登录请求体（对齐 backend/app/schemas/auth.py: LoginRequest） */
export interface LoginRequest {
  email: string
  password: string
}

/** 登录响应体（对齐 backend/app/schemas/auth.py: TokenResponse） */
export interface TokenResponse {
  access_token: string
  token_type: string
}

/** 登录，成功返回 JWT 访问令牌 */
export async function login(payload: LoginRequest): Promise<TokenResponse> {
  const { data } = await request.post<TokenResponse>('/auth/login', payload)
  return data
}
