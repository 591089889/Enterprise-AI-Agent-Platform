/**
 * 知识库 Store（Stage 16）。
 *
 * 设计原则（与 stores/agent.ts 保持一致）
 * --------------------------------------
 * - 页面只读 store，不直接调用 ``api/*``；
 * - 错误统一在 store 内归一化为「可直接展示的文案」（``utils/error.ts``）；
 * - ``loading``：知识库列表加载中；``documentsLoading``：文档列表加载中；
 *   ``submitting``：写操作进行中（按钮禁用）；
 * - 写操作成功后刷新对应列表，保证前端与后端一致；
 * - 索引结果（成功/失败）由调用方读取返回值展示，**不做乐观假设**。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  createDocument as createDocumentApi,
  createKnowledgeBase as createKnowledgeBaseApi,
  createKnowledgeBaseWithUpload as createKnowledgeBaseWithUploadApi,
  deleteDocument as deleteDocumentApi,
  deleteKnowledgeBase as deleteKnowledgeBaseApi,
  getDocument as getDocumentApi,
  indexDocument as indexDocumentApi,
  listDocuments,
  listKnowledgeBases,
  updateDocument as updateDocumentApi,
  updateKnowledgeBase as updateKnowledgeBaseApi,
  uploadDocument as uploadDocumentApi,
  type DocumentDetail,
  type DocumentIndexResult,
  type DocumentPayload,
  type DocumentSummary,
  type KnowledgeBase,
  type KnowledgeBasePayload,
} from '../api/knowledgeBase'
import { errorMessage } from '../utils/error'

export const useKnowledgeBaseStore = defineStore('knowledgeBase', () => {
  /** 知识库列表 */
  const knowledgeBases = ref<KnowledgeBase[]>([])
  /** 当前选中知识库的文档列表（不含正文） */
  const documents = ref<DocumentSummary[]>([])
  /** 当前正在查看文档的知识库 ID（文档写操作后据此刷新） */
  const currentKnowledgeBaseId = ref<number | null>(null)

  const loading = ref(false)
  const documentsLoading = ref(false)
  const submitting = ref(false)
  /** 文件上传中（Stage 22：上传按钮禁用） */
  const uploading = ref(false)
  const error = ref<string | null>(null)

  /** 知识库列表为空（加载结束后） */
  const isEmpty = computed(
    () => !loading.value && knowledgeBases.value.length === 0,
  )
  /** 文档列表为空（加载结束后） */
  const documentsEmpty = computed(
    () => !documentsLoading.value && documents.value.length === 0,
  )

  /** 加载知识库列表 */
  async function loadKnowledgeBases(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      knowledgeBases.value = await listKnowledgeBases()
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      loading.value = false
    }
  }

  /** 创建知识库（成功返回新对象并刷新列表） */
  async function createKnowledgeBase(
    payload: KnowledgeBasePayload,
  ): Promise<KnowledgeBase | null> {
    submitting.value = true
    error.value = null
    try {
      const created = await createKnowledgeBaseApi(payload)
      await loadKnowledgeBases()
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /**
   * 创建知识库并同时导入首份文档（Stage 23，``multipart/form-data``）。
   *
   * 与 :func:`createKnowledgeBase` 的区别：文件解析也发生在**同一个请求**里，
   * 因此不存在「库建好了但文档失败」的中间态 —— 失败时前端拿不到任何半成品，
   * 且 ``error`` 会带上后端返回的原因（类型不支持 / 空文件 / 解析失败 / 超过大小上限）。
   * 成功后刷新知识库列表；新文档为 ``pending``，索引仍需在详情页显式触发。
   */
  async function createKnowledgeBaseWithUpload(
    payload: KnowledgeBasePayload,
    file: File,
  ): Promise<KnowledgeBase | null> {
    uploading.value = true
    error.value = null
    try {
      const created = await createKnowledgeBaseWithUploadApi(payload, file)
      await loadKnowledgeBases()
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      uploading.value = false
    }
  }

  /** 更新知识库（成功返回更新后的对象并刷新列表） */
  async function updateKnowledgeBase(
    knowledgeBaseId: number,
    payload: Partial<KnowledgeBasePayload>,
  ): Promise<KnowledgeBase | null> {
    submitting.value = true
    error.value = null
    try {
      const updated = await updateKnowledgeBaseApi(knowledgeBaseId, payload)
      await loadKnowledgeBases()
      return updated
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 删除知识库（其文档由数据库级联删除） */
  async function removeKnowledgeBase(
    knowledgeBaseId: number,
  ): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      await deleteKnowledgeBaseApi(knowledgeBaseId)
      knowledgeBases.value = knowledgeBases.value.filter(
        (item) => item.id !== knowledgeBaseId,
      )
      if (currentKnowledgeBaseId.value === knowledgeBaseId) {
        documents.value = []
        currentKnowledgeBaseId.value = null
      }
      await loadKnowledgeBases()
      return true
    } catch (err) {
      error.value = errorMessage(err)
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 加载指定知识库的文档列表 */
  async function loadDocuments(knowledgeBaseId: number): Promise<void> {
    currentKnowledgeBaseId.value = knowledgeBaseId
    documentsLoading.value = true
    error.value = null
    try {
      documents.value = await listDocuments(knowledgeBaseId)
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      documentsLoading.value = false
    }
  }

  /** 读取文档详情（含正文；查看/编辑时加载最新内容） */
  async function fetchDocument(
    knowledgeBaseId: number,
    documentId: number,
  ): Promise<DocumentDetail | null> {
    error.value = null
    try {
      return await getDocumentApi(knowledgeBaseId, documentId)
    } catch (err) {
      error.value = errorMessage(err)
      return null
    }
  }

  /** 创建文档（成功后刷新文档列表） */
  async function createDocument(
    knowledgeBaseId: number,
    payload: DocumentPayload,
  ): Promise<DocumentDetail | null> {
    submitting.value = true
    error.value = null
    try {
      const created = await createDocumentApi(knowledgeBaseId, payload)
      await loadDocuments(knowledgeBaseId)
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /**
   * 上传文件创建文档（Stage 22，成功后刷新文档列表）。
   *
   * 与 :func:`createDocument` 的唯一区别是正文来源：由后端从文件中提取。
   * 返回真实的文档对象（``index_status=pending``）——**不做乐观假设**，
   * 索引仍需调用方显式触发。
   */
  async function uploadDocument(
    knowledgeBaseId: number,
    file: File,
  ): Promise<DocumentDetail | null> {
    uploading.value = true
    error.value = null
    try {
      const created = await uploadDocumentApi(knowledgeBaseId, file)
      await loadDocuments(knowledgeBaseId)
      return created
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      uploading.value = false
    }
  }

  /** 更新文档（成功后刷新文档列表） */
  async function updateDocument(
    knowledgeBaseId: number,
    documentId: number,
    payload: Partial<DocumentPayload>,
  ): Promise<DocumentDetail | null> {
    submitting.value = true
    error.value = null
    try {
      const updated = await updateDocumentApi(
        knowledgeBaseId,
        documentId,
        payload,
      )
      await loadDocuments(knowledgeBaseId)
      return updated
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 删除文档（后端会同步重建向量索引；成功后刷新列表） */
  async function removeDocument(
    knowledgeBaseId: number,
    documentId: number,
  ): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      await deleteDocumentApi(knowledgeBaseId, documentId)
      await loadDocuments(knowledgeBaseId)
      return true
    } catch (err) {
      error.value = errorMessage(err)
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 触发索引（返回真实结果：indexed / failed；成功后刷新文档列表） */
  async function indexDocument(
    knowledgeBaseId: number,
    documentId: number,
  ): Promise<DocumentIndexResult | null> {
    submitting.value = true
    error.value = null
    try {
      const result = await indexDocumentApi(knowledgeBaseId, documentId)
      await loadDocuments(knowledgeBaseId)
      return result
    } catch (err) {
      error.value = errorMessage(err)
      return null
    } finally {
      submitting.value = false
    }
  }

  /** 清空错误提示 */
  function clearError(): void {
    error.value = null
  }

  /** 清空全部状态（登出时调用） */
  function reset(): void {
    knowledgeBases.value = []
    documents.value = []
    currentKnowledgeBaseId.value = null
    loading.value = false
    documentsLoading.value = false
    submitting.value = false
    uploading.value = false
    error.value = null
  }

  return {
    knowledgeBases,
    documents,
    currentKnowledgeBaseId,
    loading,
    documentsLoading,
    submitting,
    uploading,
    error,
    isEmpty,
    documentsEmpty,
    loadKnowledgeBases,
    createKnowledgeBase,
    createKnowledgeBaseWithUpload,
    updateKnowledgeBase,
    removeKnowledgeBase,
    loadDocuments,
    fetchDocument,
    createDocument,
    uploadDocument,
    updateDocument,
    removeDocument,
    indexDocument,
    clearError,
    reset,
  }
})

