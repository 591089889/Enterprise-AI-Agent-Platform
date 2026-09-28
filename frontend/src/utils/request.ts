/**
 * 统一 Axios 实例（全项目唯一的 HTTP 出口）。
 *
 * 职责
 * ----
 * 1. **Base URL**：读取 ``VITE_API_BASE_URL``，未配置时回退本地后端；
 * 2. **认证**：请求拦截器自动附加 ``Authorization: Bearer <token>``；
 * 3. **错误归一化**：把后端 Stage 10 的统一错误结构
 *
 *        {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "..."}}
 *
 *    转换成前端统一的 :class:`ApiError`（UI 只展示 ``message``，
 *    分支判断只依赖 ``code`` / ``status``，不解析/展示后端原始 JSON）；
 * 4. **401 处理**：清除失效 token，并调用由 ``main.ts`` 注册的处理器
 *    （清 store + 跳回登录页）。
 *
 * 约定：所有页面/组件都通过 ``src/api/*.ts`` 调用本实例，
 * 不允许在页面里新建 Axios 实例。
 */

import axios, { type AxiosError } from 'axios'

import { clearToken, getToken } from './authToken'

/**
 * 后端 API 地址。
 *
 * ``frontend/.env`` 中未配置 ``VITE_API_BASE_URL`` 时回退到 Enterprise 后端
 * 默认开发端口（8001），避免 baseURL 为 undefined 时请求打到前端 dev
 * server（5174）而 404。
 */
export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8001'

/** 后端统一错误响应体（Stage 10） */
interface ApiErrorPayload {
  error?: {
    code?: unknown
    message?: unknown
  }
}

/** HTTP 状态码 → 兜底文案（后端未返回 message 时使用，避免展示原始 JSON） */
const FALLBACK_MESSAGES: Record<number, string> = {
  400: '请求无效，请检查输入内容',
  401: '登录状态已失效，请重新登录',
  403: '没有权限执行该操作',
  404: '请求的资源不存在',
  405: '请求方法不被支持',
  409: '操作与现有数据冲突',
  422: '请求参数校验失败',
  429: '操作过于频繁，请稍后重试',
  500: '服务器内部错误，请稍后重试',
  502: 'AI 模型服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
}

/** 网络层失败（未收到响应）时的文案 */
const NETWORK_ERROR_MESSAGE = '无法连接到服务器，请确认后端服务已启动'

/**
 * 前端统一错误对象。
 *
 * 页面只应该展示 ``message``；需要区分的场景按 ``code`` / ``status`` 分支。
 */
export class ApiError extends Error {
  /** HTTP 状态码；网络层失败时为 0 */
  readonly status: number
  /** 后端稳定错误码（如 AUTHENTICATION_REQUIRED / RESOURCE_NOT_FOUND） */
  readonly code: string

  constructor(message: string, status = 0, code = 'UNKNOWN_ERROR') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** 是否为网络层失败（后端未启动 / 跨域 / 超时） */
  get isNetworkError(): boolean {
    return this.status === 0
  }
}

/** 401（登录接口自身除外）时的回调：清 store + 回登录页 */
type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/**
 * 注册 401 处理器（在 ``main.ts`` 中注册，避免 request.ts 依赖 Pinia）。
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler
}

/** 从后端错误响应体中提取 code / message */
function readErrorPayload(data: unknown): { code?: string; message?: string } {
  if (typeof data !== 'object' || data === null) {
    return {}
  }
  const errorField = (data as ApiErrorPayload).error
  if (typeof errorField !== 'object' || errorField === null) {
    return {}
  }
  return {
    code: typeof errorField.code === 'string' ? errorField.code : undefined,
    message:
      typeof errorField.message === 'string' ? errorField.message : undefined,
  }
}

/** 登录接口的 401 属于「凭证错误」，不能触发「登录失效 → 跳转登录页」 */
function isLoginRequest(url: string | undefined): boolean {
  return typeof url === 'string' && url.endsWith('/auth/login')
}

/** 把 Axios 错误转换为 :class:`ApiError` */
function toApiError(error: AxiosError): ApiError {
  // 网络层失败：没有 response（后端未启动 / 连接被拒 / 超时）
  if (!error.response) {
    return new ApiError(NETWORK_ERROR_MESSAGE, 0, 'NETWORK_ERROR')
  }

  const { status, data } = error.response
  const payload = readErrorPayload(data)
  const message =
    payload.message ?? FALLBACK_MESSAGES[status] ?? `请求失败（HTTP ${status}）`
  const code = payload.code ?? 'HTTP_ERROR'

  return new ApiError(message, status, code)
}

/** 创建 Axios 实例 */
const request = axios.create({
  baseURL: API_BASE_URL,
  // Agent 对话需要等待 LLM 生成，超时时间相对普通的 CRUD 更长
  timeout: 60000,
})

// 请求拦截器：自动附加 Bearer Token
request.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      config.headers.set('Authorization', `Bearer ${token}`)
    }
    return config
  },
  (error: unknown) => Promise.reject(error),
)

// 响应拦截器：错误归一化 + 401 处理
request.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (!axios.isAxiosError(error)) {
      return Promise.reject(new ApiError('发生未知错误，请稍后重试'))
    }

    const apiError = toApiError(error)

    if (import.meta.env.DEV) {
      // 开发环境保留日志便于排查；UI 展示的始终是 apiError.message
      console.debug(
        `[api] ${error.config?.method?.toUpperCase() ?? ''} ${error.config?.url ?? ''} → ${apiError.status} ${apiError.code}: ${apiError.message}`,
      )
    }

    if (apiError.status === 401 && !isLoginRequest(error.config?.url)) {
      // Token 无效 / 过期：清理本地 token，并交给已注册的处理器（清 store + 跳登录页）
      clearToken()
      unauthorizedHandler?.()
    }

    return Promise.reject(apiError)
  },
)

export default request
