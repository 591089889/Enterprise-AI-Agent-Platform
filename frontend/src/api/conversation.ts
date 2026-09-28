/**
 * 会话 API：/conversations/*
 *
 * 覆盖：
 * - POST   /conversations                    创建会话
 * - GET    /conversations?page&page_size     分页列表
 * - GET    /conversations/{id}               会话详情
 * - GET    /conversations/{id}/messages      历史消息（只读）
 * - PATCH  /conversations/{id}               重命名
 * - DELETE /conversations/{id}               删除（204，无响应体）
 *
 * Schema 全部对齐 backend/app/schemas/conversation.py 与 schemas/message.py。
 */

import request from '../utils/request'

/** 会话（对齐 ConversationResponse） */
export interface Conversation {
  id: number
  user_id: number
  title: string
  created_at: string
  updated_at: string
}

/** 会话分页响应（对齐 PaginatedConversationsResponse） */
export interface PaginatedConversations {
  items: Conversation[]
  total: number
  page: number
  page_size: number
}

/** 历史消息（对齐 MessageResponse） */
export interface Message {
  id: number
  conversation_id: number
  role: string
  content: string
  created_at: string
}

/** 创建会话（title 可省略，后端默认空字符串） */
export async function createConversation(title = ''): Promise<Conversation> {
  const { data } = await request.post<Conversation>('/conversations', { title })
  return data
}

/** 分页获取当前用户的会话列表 */
export async function listConversations(params: {
  page: number
  page_size: number
}): Promise<PaginatedConversations> {
  const { data } = await request.get<PaginatedConversations>('/conversations', {
    params,
  })
  return data
}

/** 获取会话详情 */
export async function getConversation(conversationId: number): Promise<Conversation> {
  const { data } = await request.get<Conversation>(
    `/conversations/${conversationId}`,
  )
  return data
}

/** 重命名会话 */
export async function renameConversation(
  conversationId: number,
  title: string,
): Promise<Conversation> {
  const { data } = await request.patch<Conversation>(
    `/conversations/${conversationId}`,
    { title },
  )
  return data
}

/** 删除会话（后端返回 204，无响应体） */
export async function deleteConversation(conversationId: number): Promise<void> {
  await request.delete(`/conversations/${conversationId}`)
}

/** 获取会话历史消息（按时间正序） */
export async function listMessages(conversationId: number): Promise<Message[]> {
  const { data } = await request.get<Message[]>(
    `/conversations/${conversationId}/messages`,
  )
  return data
}
