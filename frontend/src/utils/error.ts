/**
 * 把任意异常转换为「可以直接显示给用户」的文案。
 *
 * Axios 错误已经在 ``utils/request.ts`` 的响应拦截器里归一化为
 * :class:`ApiError`，因此这里只需处理 ApiError / Error / 未知值三种情况。
 */

import { ApiError } from './request'

/** 兜底文案（既不是 ApiError 也不是 Error 时使用） */
const UNKNOWN_MESSAGE = '发生未知错误，请稍后重试'

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error && error.message) {
    return error.message
  }
  return UNKNOWN_MESSAGE
}

export { ApiError }
