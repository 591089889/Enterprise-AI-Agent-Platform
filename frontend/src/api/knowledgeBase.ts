/**
 * 知识库管理 API：``/knowledge-bases/*``（Stage 16）
 *
 * 职责
 * ----
 * 知识库（Knowledge Base）与文档（Document）的管理接口 + 文档索引触发。
 *
 * 权限：这些接口**只允许管理员**调用（未登录 401 / 普通用户 403），
 * 由后端 ``require_admin`` 强制校验；前端路由守卫只是体验优化。
 *
 * 与 AI 工作台的关系：本模块不参与对话检索（Agent ↔ 知识库绑定属后续阶段）。
 *
 * 请求统一走 ``utils/request.ts``（自动附加 Bearer + 错误归一化为 ApiError）。
 */

import request from '../utils/request'

/** 知识库（对齐 backend/app/schemas/knowledge_base.py: KnowledgeBaseResponse） */
export interface KnowledgeBase {
  id: number
  name: string
  description: string
  created_at: string
  updated_at: string
}

/** 创建 / 更新知识库的请求体 */
export interface KnowledgeBasePayload {
  name: string
  description: string
}

/** 文档列表项（对齐 DocumentSummary：**不含正文**） */
export interface DocumentSummary {
  id: number
  knowledge_base_id: number
  name: string
  /** pending / indexed / failed */
  index_status: string
  created_at: string
  updated_at: string
}

/** 文档详情（对齐 DocumentDetail：含完整正文） */
export interface DocumentDetail extends DocumentSummary {
  content: string
}

/** 创建 / 更新文档的请求体 */
export interface DocumentPayload {
  name: string
  content: string
}

/** 索引结果（对齐 DocumentIndexResponse） */
export interface DocumentIndexResult {
  document_id: number
  /** indexed / failed（唯一真源，绝不谎报成功） */
  status: string
  message: string
}

// ------------------------------------------------------------------
// 知识库
// ------------------------------------------------------------------
/** 获取知识库列表 */
export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  const { data } = await request.get<KnowledgeBase[]>('/knowledge-bases')
  return data
}

/** 获取单个知识库 */
export async function getKnowledgeBase(
  knowledgeBaseId: number,
): Promise<KnowledgeBase> {
  const { data } = await request.get<KnowledgeBase>(
    `/knowledge-bases/${knowledgeBaseId}`,
  )
  return data
}

/** 创建知识库 */
export async function createKnowledgeBase(
  payload: KnowledgeBasePayload,
): Promise<KnowledgeBase> {
  const { data } = await request.post<KnowledgeBase>(
    '/knowledge-bases',
    payload,
  )
  return data
}

/**
 * 创建知识库并同时导入首份文档（Stage 23，``multipart/form-data``）。
 *
 * - 与 :func:`createKnowledgeBase` 是**同一个端点**：只是把请求体换成表单，
 *   ``file`` 作为可选的「首份文档」；不手动设置 ``Content-Type``，
 *   交给 axios 生成带 boundary 的 multipart 头；
 * - 后端在**同一个请求**内完成「建库 + 解析文件为文档正文」，因此**要么都成功、
 *   要么都不发生**（文件类型不支持 / 解析失败时整体回滚，不会留半成品知识库）；
 * - 返回的仍是知识库对象；新文档 ``index_status=pending``，
 *   需在详情页显式触发索引；**原始文件不会被保存**。
 */
export async function createKnowledgeBaseWithUpload(
  payload: KnowledgeBasePayload,
  file: File,
): Promise<KnowledgeBase> {
  const formData = new FormData()
  formData.append('name', payload.name)
  formData.append('description', payload.description)
  formData.append('file', file)
  const { data } = await request.post<KnowledgeBase>(
    '/knowledge-bases',
    formData,
  )
  return data
}

/** 更新知识库（部分更新） */
export async function updateKnowledgeBase(
  knowledgeBaseId: number,
  payload: Partial<KnowledgeBasePayload>,
): Promise<KnowledgeBase> {
  const { data } = await request.patch<KnowledgeBase>(
    `/knowledge-bases/${knowledgeBaseId}`,
    payload,
  )
  return data
}

/** 删除知识库（后端返回 204，其文档级联删除） */
export async function deleteKnowledgeBase(
  knowledgeBaseId: number,
): Promise<void> {
  await request.delete(`/knowledge-bases/${knowledgeBaseId}`)
}

// ------------------------------------------------------------------
// 文档
// ------------------------------------------------------------------
/** 获取文档列表（不含正文） */
export async function listDocuments(
  knowledgeBaseId: number,
): Promise<DocumentSummary[]> {
  const { data } = await request.get<DocumentSummary[]>(
    `/knowledge-bases/${knowledgeBaseId}/documents`,
  )
  return data
}

/** 获取文档详情（含正文） */
export async function getDocument(
  knowledgeBaseId: number,
  documentId: number,
): Promise<DocumentDetail> {
  const { data } = await request.get<DocumentDetail>(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`,
  )
  return data
}

/** 创建文档（只保存原始正文，索引状态为 pending） */
export async function createDocument(
  knowledgeBaseId: number,
  payload: DocumentPayload,
): Promise<DocumentDetail> {
  const { data } = await request.post<DocumentDetail>(
    `/knowledge-bases/${knowledgeBaseId}/documents`,
    payload,
  )
  return data
}

/**
 * 上传文件创建文档（Stage 22，``multipart/form-data``）。
 *
 * - 前端**不解析文件**：只把原始文件交给后端，由后端提取纯文本后保存为正文；
 * - 不手动设置 ``Content-Type``：交给 axios 生成带 boundary 的 multipart 头；
 * - 返回的文档 ``index_status`` 为 ``pending``（索引仍需显式触发）。
 */
export async function uploadDocument(
  knowledgeBaseId: number,
  file: File,
): Promise<DocumentDetail> {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await request.post<DocumentDetail>(
    `/knowledge-bases/${knowledgeBaseId}/documents/upload`,
    formData,
  )
  return data
}

/** 更新文档（修改正文会重置索引状态为 pending） */
export async function updateDocument(
  knowledgeBaseId: number,
  documentId: number,
  payload: Partial<DocumentPayload>,
): Promise<DocumentDetail> {
  const { data } = await request.patch<DocumentDetail>(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`,
    payload,
  )
  return data
}

/** 删除文档（后端会同步重建该知识库的向量索引） */
export async function deleteDocument(
  knowledgeBaseId: number,
  documentId: number,
): Promise<void> {
  await request.delete(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`,
  )
}

/** 建立 / 重新建立文档索引（返回真实状态，失败时 status=failed） */
export async function indexDocument(
  knowledgeBaseId: number,
  documentId: number,
): Promise<DocumentIndexResult> {
  const { data } = await request.post<DocumentIndexResult>(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/index`,
  )
  return data
}
