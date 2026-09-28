/**
 * Agent API：对话执行 ``POST /agent/chat`` + 配置管理 ``/agents/*``
 *
 * 两部分职责分离（同一 Agent 领域，统一放在本文件）：
 *
 * 1. **对话执行**（``sendAgentMessage`` → ``POST /agent/chat``）：
 *    请求可携带 ``agent_id``（Stage 17）——后端按该 Agent 的
 *    provider / model / temperature / system_prompt / 知识库 / 工具绑定执行；
 *    不携带时沿用平台默认配置。RAG 检索、Tool Calling、Provider 选择
 *    全部由后端 Agent Service / Runtime 内部决定，前端不感知细节；
 * 2. **配置管理**（``/agents/*``，Stage 15 + Stage 17）：
 *    - ``/agents``（CRUD）与 ``/agents/{id}``：**仅管理员**，Agent 管理页面使用；
 *    - ``/agents/available``：**任意登录用户**，只返回 id/name/description，
 *      供 AI 工作台的 Agent 选择器使用（不暴露 system_prompt 等管理配置）。
 */

import request from '../utils/request'

/** 对话请求体（对齐 backend/app/schemas/agent.py: AgentChatRequest） */
export interface AgentChatRequest {
  conversation_id: number
  /**
   * 使用的 Agent 配置 ID（可选，Stage 17）。
   *
   * - 指定时：使用该 Agent 的 provider / model / temperature /
   *   system_prompt / 知识库 / 工具绑定执行；
   * - 不指定时：沿用平台默认配置（backend/.env 的全局设置）。
   */
  agent_id?: number
  /** 用户输入消息（后端限制 1 - 4000 字符） */
  message: string
}

/** 对话响应体（对齐 backend/app/schemas/agent.py: AgentChatResponse） */
export interface AgentChatResponse {
  conversation_id: number
  reply: string
}

/**
 * 发送消息给 AI Agent（对话执行）。
 *
 * 可能的错误（由后端统一错误结构返回，前端只展示 message）：
 * - 404 RESOURCE_NOT_FOUND       会话不存在
 * - 403 PERMISSION_DENIED        会话不属于当前用户
 * - 502 UPSTREAM_SERVICE_ERROR   上游 LLM 服务不可用
 */
export async function sendAgentMessage(
  payload: AgentChatRequest,
): Promise<AgentChatResponse> {
  const { data } = await request.post<AgentChatResponse>('/agent/chat', payload)
  return data
}

/**
 * Agent 配置管理 API：``/agents/*``（Stage 15）
 *
 * 与文件上方 ``/agent/chat`` 的分工：
 * - ``POST /agent/chat``：Agent 对话**执行**（AI 工作台使用）；
 * - ``/agents/*``       ：Agent **配置管理**（Agent 管理页面使用）。
 *
 * 权限：这些接口只允许管理员调用（未登录 401 / 普通用户 403），
 * 由后端 ``require_admin`` 强制校验；前端路由守卫只是体验优化。
 *
 * 请求统一走 ``utils/request.ts``（自动附加 Bearer + 错误归一化为 ApiError）。
 */

/** Agent 配置（对齐 backend/app/schemas/agent.py: AgentResponse） */
export interface AgentConfig {
  id: number
  name: string
  description: string
  system_prompt: string
  provider: string
  model: string
  temperature: number
  /** 绑定的知识库 ID（null 表示不使用 RAG，Stage 17） */
  knowledge_base_id: number | null
  /** 该 Agent 可用的工具名称列表（空表示不向 LLM 暴露工具，Stage 17） */
  tool_names: string[]
  created_at: string
  updated_at: string
}

/** 创建 Agent 的请求体（对齐 AgentCreate） */
export interface AgentCreatePayload {
  name: string
  description: string
  system_prompt: string
  provider: string
  model: string
  temperature: number
  knowledge_base_id: number | null
  tool_names: string[]
}

/** 更新 Agent 的请求体（对齐 AgentUpdate：全部字段可选，只传需要修改的） */
export type AgentUpdatePayload = Partial<AgentCreatePayload>

/**
 * Agent 选择项（对齐 AgentAvailableResponse，Stage 17）
 *
 * 只含 id / name / description —— 供 AI 工作台的 Agent 选择器使用，
 * 不包含 system_prompt / provider 等管理配置。
 */
export interface AgentAvailable {
  id: number
  name: string
  description: string
}

/**
 * 获取可供对话使用的 Agent 列表（任意登录用户可调用）。
 *
 * 与 ``listAgents()`` 的区别：``listAgents()`` 是管理接口（仅管理员），
 * 本函数返回最小字段集，普通用户在 Chat 页面用它填充 Agent 选择器。
 */
export async function listAvailableAgents(): Promise<AgentAvailable[]> {
  const { data } = await request.get<AgentAvailable[]>('/agents/available')
  return data
}

/** 可绑定工具（对齐 backend/app/schemas/agent.py: AgentToolOption） */
export interface AgentToolOption {
  name: string
  description: string
}

/**
 * 获取可绑定的工具列表（仅管理员）。
 *
 * 数据来源是后端内置 ToolRegistry，因此页面展示的可选项与后端校验的
 * 合法集合一致（不会出现「页面能勾选、后端拒绝」的名单漂移）。
 */
export async function listAgentTools(): Promise<AgentToolOption[]> {
  const { data } = await request.get<AgentToolOption[]>('/agents/tools')
  return data
}

/** 获取 Agent 配置列表（仅返回当前后端保存的配置，不调用 LLM） */
export async function listAgents(): Promise<AgentConfig[]> {
  const { data } = await request.get<AgentConfig[]>('/agents')
  return data
}

/** 获取单个 Agent 配置（编辑时加载最新数据） */
export async function getAgent(agentId: number): Promise<AgentConfig> {
  const { data } = await request.get<AgentConfig>(`/agents/${agentId}`)
  return data
}

/** 创建 Agent 配置（成功后返回完整对象，含 id 与时间戳） */
export async function createAgent(
  payload: AgentCreatePayload,
): Promise<AgentConfig> {
  const { data } = await request.post<AgentConfig>('/agents', payload)
  return data
}

/** 更新 Agent 配置（部分更新；只发送传入的字段） */
export async function updateAgent(
  agentId: number,
  payload: AgentUpdatePayload,
): Promise<AgentConfig> {
  const { data } = await request.patch<AgentConfig>(
    `/agents/${agentId}`,
    payload,
  )
  return data
}

/** 删除 Agent 配置（后端返回 204，无响应体） */
export async function deleteAgent(agentId: number): Promise<void> {
  await request.delete(`/agents/${agentId}`)
}
