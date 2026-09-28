/**
 * 访问令牌本地存储。
 *
 * 为什么要独立成一个模块（而不是放在 Pinia store 里）？
 * --------------------------------------------------
 * ``utils/request.ts`` 的请求拦截器必须能拿到 token，但 request.ts
 * 不应该反向依赖 Pinia store（否则 request.ts ← stores/auth.ts ← api/auth.ts
 * ← request.ts 形成循环依赖）。
 *
 * 因此这里只做「纯函数 + localStorage 读写」：
 * - request.ts 读 token（附带 Authorization 头）；
 * - stores/auth.ts 写 / 清 token（登录、登出、401 失效）。
 *
 * 存储位置：localStorage（刷新页面后仍保持登录状态）。
 */

/** localStorage 中保存 access token 的 key */
const TOKEN_STORAGE_KEY = 'eaap.access_token'

/** 读取本地保存的 access token（不存在时返回 null） */
export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    // 隐私模式 / 禁用 localStorage 时降级为「无 token」
    return null
  }
}

/** 保存 access token */
export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // 写入失败不影响当前会话内使用（内存中的 store 仍持有 token）
  }
}

/** 清除 access token（登出 / 401 失效时调用） */
export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // 忽略：清不掉也不会影响后续流程
  }
}
