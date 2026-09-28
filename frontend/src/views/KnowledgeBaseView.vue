<script setup lang="ts">
/**
 * 知识库管理页面（Stage 16）。
 *
 * 位置 / 权限
 * -----------
 * 平台外壳内的 ``/app/knowledge`` 模块页面，仅管理员可进入
 * （``router/index.ts`` 的 ``meta.requiresAdmin``；后端同样 require_admin）。
 *
 * 两种视图（同一组件，靠 URL 查询参数区分，避免新增路由）
 * --------------------------------------------------------
 * - 列表视图：``/app/knowledge``          —— 知识库的增删改查
 * - 详情视图：``/app/knowledge?kb={id}``  —— 该知识库的文档管理 + 索引
 *
 * 职责
 * ----
 * 管理**知识库与文档**（原始正文），并按需触发索引：
 *
 *     知识库 → 文档（原始正文） → Index → 现有 RAG 引擎（切分 / 向量化 / 向量库）
 *
 * 文档正文有两个来源：**直接输入**，或**上传文件**（Stage 22：``.txt`` / ``.md`` /
 * ``.pdf`` / ``.docx``）。上传时前端只发送原始文件，解析全部在后端完成，
 * 原始文件不会被保存（后端解析后立即删除，数据库只保留提取出的正文）。
 *
 * 不参与对话检索（Agent ↔ 知识库绑定属后续阶段）；前端不实现任何向量逻辑。
 *
 * 数据流：本页面 → stores/knowledgeBase.ts → api/knowledgeBase.ts → FastAPI
 */

import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type {
  DocumentDetail,
  DocumentSummary,
  KnowledgeBase,
} from '../api/knowledgeBase'
import { useKnowledgeBaseStore } from '../stores/knowledgeBase'

const route = useRoute()
const router = useRouter()
const store = useKnowledgeBaseStore()

/** 当前选中的知识库 ID（来自 URL 查询参数 ``?kb=``，刷新后仍可恢复） */
const selectedKbId = computed<number | null>(() => {
  const raw = route.query.kb
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || value === '') {
    return null
  }
  const parsed = Number.parseInt(value, 10)
  return Number.isNaN(parsed) ? null : parsed
})

const isDetailMode = computed(() => selectedKbId.value !== null)

/** 当前知识库（从已加载列表派生，避免重复请求） */
const currentKb = computed<KnowledgeBase | null>(
  () =>
    store.knowledgeBases.find((item) => item.id === selectedKbId.value) ?? null,
)

// ------------------------------------------------------------------
// 知识库表单（新建 / 编辑）
// ------------------------------------------------------------------
const kbFormMode = ref<'create' | 'edit' | null>(null)
const kbFormError = ref<string | null>(null)
const kbForm = reactive({
  id: null as number | null,
  name: '',
  description: '',
})

const isKbFormOpen = computed(() => kbFormMode.value !== null)
const kbFormTitle = computed(() =>
  kbFormMode.value === 'edit' ? '编辑知识库' : '新建知识库',
)
/** 新建知识库时可选的「首份文档」（仅 create 模式使用；未选择时为 null） */
const kbFormFile = ref<File | null>(null)
/** 首份文档选择提示（体验层校验；类型 / 大小 / 解析最终以后端为准） */
const kbFormFileNotice = ref<string | null>(null)
/** 「创建并导入首份文档」成功后的提示（与表单错误分开，切换视图后仍可见） */
const kbCreatedNotice = ref<string | null>(null)
/** 提交按钮文案（带文件时明确表达「创建 + 导入」） */
const submitKbLabel = computed(() => {
  if (store.uploading) {
    return '创建并导入中…'
  }
  if (store.submitting) {
    return '保存中…'
  }
  return kbFormMode.value === 'create' && kbFormFile.value !== null
    ? '创建并导入首份文档'
    : '保存'
})

function openCreateKb(): void {
  kbFormMode.value = 'create'
  kbForm.id = null
  kbForm.name = ''
  kbForm.description = ''
  kbFormError.value = null
  kbFormFile.value = null
  kbFormFileNotice.value = null
  kbCreatedNotice.value = null
}

function openEditKb(knowledgeBase: KnowledgeBase): void {
  kbFormMode.value = 'edit'
  kbForm.id = knowledgeBase.id
  kbForm.name = knowledgeBase.name
  kbForm.description = knowledgeBase.description
  kbFormError.value = null
  kbFormFile.value = null
  kbFormFileNotice.value = null
}

function closeKbForm(): void {
  kbFormMode.value = null
  kbFormError.value = null
  kbFormFile.value = null
  kbFormFileNotice.value = null
}

/**
 * 选择「首份文档」（仅体验层校验扩展名）。
 *
 * 与详情页的上传不同：这里**不立即上传**，文件随创建请求一起提交
 * （后端在同一个请求内建库 + 解析），因此保留 ``input.value`` 以便用户改选。
 */
function onKbFileSelected(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  kbFormFileNotice.value = null
  if (file === null) {
    kbFormFile.value = null
    return
  }

  const lowerName = file.name.toLowerCase()
  if (!ACCEPTED_FILE_EXTENSIONS.some((ext) => lowerName.endsWith(ext))) {
    kbFormFile.value = null
    kbFormFileNotice.value = `不支持的文件类型，仅支持 ${ACCEPTED_FILE_EXTENSIONS.join(' / ')}`
    // 清空 value：否则用户重新选择同一个非法文件不会再触发 change
    input.value = ''
    return
  }

  kbFormFile.value = file
  kbFormFileNotice.value = `已选择「${file.name}」，提交后将作为首份文档导入`
}

async function onSubmitKb(): Promise<void> {
  if (store.submitting || store.uploading) {
    return
  }
  const name = kbForm.name.trim()
  if (name === '') {
    kbFormError.value = '请填写知识库名称'
    return
  }
  kbFormError.value = null

  const editingId = kbForm.id
  const payload = { name, description: kbForm.description.trim() }
  const fileToImport = editingId === null ? kbFormFile.value : null

  let result: KnowledgeBase | null
  if (editingId !== null) {
    result = await store.updateKnowledgeBase(editingId, payload)
  } else if (fileToImport !== null) {
    // 创建 + 首份文档：后端在同一个请求内完成，失败则整体回滚（不留半成品）
    result = await store.createKnowledgeBaseWithUpload(payload, fileToImport)
  } else {
    result = await store.createKnowledgeBase(payload)
  }

  if (result === null) {
    kbFormError.value = store.error ?? '保存失败，请重试'
    return
  }

  closeKbForm()
  if (fileToImport !== null) {
    kbCreatedNotice.value =
      `已创建知识库「${result.name}」并导入首份文档「${fileToImport.name}」：` +
      '文档当前为「待索引」状态，请点击「重新索引」写入向量库（原始文件不会被保存）。'
    // 导入后紧接的动作是「索引该文档」，直接进入详情页继续操作
    openDetail(result)
  }
}

async function onDeleteKb(knowledgeBase: KnowledgeBase): Promise<void> {
  const confirmed = window.confirm(
    `确认删除知识库「${knowledgeBase.name}」？其下的全部文档会一并删除，该操作不可撤销。`,
  )
  if (!confirmed) {
    return
  }
  const ok = await store.removeKnowledgeBase(knowledgeBase.id)
  if (ok && selectedKbId.value === knowledgeBase.id) {
    backToList()
  }
}

// ------------------------------------------------------------------
// 文档表单（新建 / 编辑）、文档查看、索引
// ------------------------------------------------------------------
const docFormMode = ref<'create' | 'edit' | null>(null)
const docFormError = ref<string | null>(null)
const docForm = reactive({
  id: null as number | null,
  name: '',
  content: '',
})
const viewingDoc = ref<DocumentDetail | null>(null)
const indexNotice = ref<{ ok: boolean; message: string } | null>(null)
/** 文件上传结果提示（真实结果，不做乐观假设） */
const uploadNotice = ref<{ ok: boolean; message: string } | null>(null)
/** 原生文件选择框（被 CSS 隐藏，由按钮触发） */
const fileInput = ref<HTMLInputElement | null>(null)

/** 支持上传的扩展名（与后端 app/rag/loader.py 的 SUPPORTED_UPLOAD_EXTENSIONS 一致） */
const ACCEPTED_FILE_EXTENSIONS = ['.txt', '.md', '.pdf', '.docx']
/** 上传大小上限提示（与后端 settings.MAX_UPLOAD_FILE_SIZE_MB 一致；最终以后端为准） */
const MAX_UPLOAD_FILE_SIZE_MB = 10

const isDocFormOpen = computed(() => docFormMode.value !== null)
const docFormTitle = computed(() =>
  docFormMode.value === 'edit' ? '编辑文档' : '新建文档',
)

function openCreateDoc(): void {
  docFormMode.value = 'create'
  docForm.id = null
  docForm.name = ''
  docForm.content = ''
  docFormError.value = null
}

/** 打开「编辑文档」：先读取最新正文（GET 文档详情），再填充表单 */
async function openEditDoc(document: DocumentSummary): Promise<void> {
  const kbId = selectedKbId.value
  if (kbId === null) {
    return
  }
  docFormMode.value = 'edit'
  docForm.id = document.id
  docForm.name = document.name
  docForm.content = ''
  docFormError.value = null

  const detail = await store.fetchDocument(kbId, document.id)
  if (detail === null) {
    docFormError.value = store.error ?? '加载文档失败'
    return
  }
  docForm.name = detail.name
  docForm.content = detail.content
}

function closeDocForm(): void {
  docFormMode.value = null
  docFormError.value = null
}

async function onSubmitDoc(): Promise<void> {
  const kbId = selectedKbId.value
  if (kbId === null || store.submitting) {
    return
  }
  const name = docForm.name.trim()
  if (name === '') {
    docFormError.value = '请填写文档名称'
    return
  }
  if (docForm.content.trim() === '') {
    docFormError.value = '请填写文档正文'
    return
  }
  docFormError.value = null

  const payload = { name, content: docForm.content }
  const result =
    docForm.id === null
      ? await store.createDocument(kbId, payload)
      : await store.updateDocument(kbId, docForm.id, payload)

  if (result !== null) {
    closeDocForm()
  } else {
    docFormError.value = store.error ?? '保存失败，请重试'
  }
}

async function onDeleteDoc(document: DocumentSummary): Promise<void> {
  const kbId = selectedKbId.value
  if (kbId === null) {
    return
  }
  const confirmed = window.confirm(
    `确认删除文档「${document.name}」？删除后该知识库会重新建立索引。`,
  )
  if (!confirmed) {
    return
  }
  const ok = await store.removeDocument(kbId, document.id)
  if (ok && viewingDoc.value?.id === document.id) {
    viewingDoc.value = null
  }
}

/** 触发索引：展示真实结果（indexed / failed），不做乐观假设 */
async function onIndexDoc(document: DocumentSummary): Promise<void> {
  const kbId = selectedKbId.value
  if (kbId === null) {
    return
  }
  indexNotice.value = null
  const result = await store.indexDocument(kbId, document.id)
  if (result === null) {
    indexNotice.value = { ok: false, message: store.error ?? '索引失败' }
    return
  }
  indexNotice.value =
    result.status === 'indexed'
      ? {
          ok: true,
          message: `文档「${document.name}」索引完成（该知识库索引已重建）`,
        }
      : {
          ok: false,
          message: result.message || '索引失败，请检查后端 Embedding / 向量库配置',
        }
}

/** 查看文档正文（GET 文档详情） */
async function onViewDoc(document: DocumentSummary): Promise<void> {
  const kbId = selectedKbId.value
  if (kbId === null) {
    return
  }
  viewingDoc.value = null
  viewingDoc.value = await store.fetchDocument(kbId, document.id)
}

// ------------------------------------------------------------------
// 文件上传（Stage 22）
// ------------------------------------------------------------------
/** 打开系统文件选择框（原生 input 被 CSS 隐藏，用按钮触发） */
function openFilePicker(): void {
  fileInput.value?.click()
}

/**
 * 上传选中的文件。
 *
 * 前端只做「扩展名提示」这一体验层校验；类型 / 大小 / 解析校验都在后端。
 * 上传成功后由 store 刷新文档列表，这里只展示后端返回的真实状态
 * （``pending`` —— 索引仍需显式触发）。
 */
async function onFileSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  // 立即清空 value：否则连续选择同一个文件不会再触发 change
  input.value = ''

  const kbId = selectedKbId.value
  if (file === null || kbId === null) {
    return
  }

  uploadNotice.value = null

  const lowerName = file.name.toLowerCase()
  if (!ACCEPTED_FILE_EXTENSIONS.some((ext) => lowerName.endsWith(ext))) {
    uploadNotice.value = {
      ok: false,
      message: `不支持的文件类型，仅支持 ${ACCEPTED_FILE_EXTENSIONS.join(' / ')}`,
    }
    return
  }

  const created = await store.uploadDocument(kbId, file)
  if (created === null) {
    uploadNotice.value = {
      ok: false,
      message: store.error ?? '上传失败，请重试',
    }
    return
  }
  uploadNotice.value = {
    ok: true,
    message:
      `已导入「${created.name}」（${indexStatusLabel(created.index_status)}）：`
      + '原始文件不会被保存，需点击该文档的「重新索引」写入向量库',
  }
}

// ------------------------------------------------------------------
// 导航
// ------------------------------------------------------------------
function openDetail(knowledgeBase: KnowledgeBase): void {
  void router.push({
    name: 'knowledge',
    query: { kb: String(knowledgeBase.id) },
  })
}

function backToList(): void {
  viewingDoc.value = null
  closeDocForm()
  closeKbForm()
  indexNotice.value = null
  uploadNotice.value = null
  kbCreatedNotice.value = null
  void router.push({ name: 'knowledge' })
}

// ------------------------------------------------------------------
// 展示辅助
// ------------------------------------------------------------------
function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** 索引状态 → 中文文案 */
function indexStatusLabel(status: string): string {
  if (status === 'indexed') {
    return '已索引'
  }
  if (status === 'failed') {
    return '索引失败'
  }
  return '待索引'
}

/** 索引状态 → badge 样式 */
function indexStatusClass(status: string): string {
  if (status === 'indexed') {
    return 'badge--ok'
  }
  if (status === 'failed') {
    return 'badge--danger'
  }
  return 'badge--pending'
}

// ------------------------------------------------------------------
// 初始化 / 路由同步
// ------------------------------------------------------------------
onMounted(() => {
  void store.loadKnowledgeBases()
})

// 详情视图：进入 / 切换知识库时加载其文档列表
watch(
  selectedKbId,
  (knowledgeBaseId) => {
    if (knowledgeBaseId !== null) {
      void store.loadDocuments(knowledgeBaseId)
    }
  },
  { immediate: true },
)

</script>

<template>
  <section class="module-page">
    <header class="module-page__header">
      <div>
        <h2 class="module-page__title">知识库</h2>
        <p class="module-page__subtitle">管理企业 AI Agent 使用的知识资源</p>
      </div>
      <button
        v-if="!isDetailMode"
        class="btn btn--primary btn--sm"
        type="button"
        :disabled="store.submitting || isKbFormOpen"
        @click="openCreateKb"
      >
        + 新建知识库
      </button>
      <button
        v-else
        class="btn btn--ghost btn--sm"
        type="button"
        @click="backToList"
      >
        ← 返回知识库列表
      </button>
    </header>

    <div class="module-page__body module-page__body--top">
      <p v-if="store.error" class="panel-alert">
        <span class="alert alert--error">{{ store.error }}</span>
      </p>

      <p v-if="kbCreatedNotice" class="panel-alert">
        <span class="alert alert--info">{{ kbCreatedNotice }}</span>
      </p>

      <!-- 知识库表单（列表视图新建 / 详情视图编辑 共用） -->
      <div v-if="isKbFormOpen" class="panel">
        <div class="panel__header">
          <span class="panel__title">{{ kbFormTitle }}</span>
          <button
            class="btn btn--ghost btn--sm"
            type="button"
            :disabled="store.submitting"
            @click="closeKbForm"
          >
            关闭
          </button>
        </div>
        <form class="panel__body form-grid" @submit.prevent="onSubmitKb">
          <p v-if="kbFormError" class="form-grid__full panel-alert">
            <span class="alert alert--error">{{ kbFormError }}</span>
          </p>
          <label class="field">
            <span class="field__label">名称 *</span>
            <input
              v-model="kbForm.name"
              class="input"
              type="text"
              maxlength="100"
              placeholder="例如：企业产品手册"
            />
          </label>
          <label class="field form-grid__full">
            <span class="field__label">描述</span>
            <input
              v-model="kbForm.description"
              class="input"
              type="text"
              maxlength="500"
              placeholder="例如：存放产品说明与售后政策"
            />
          </label>
          <!-- 首份文档（可选，仅新建时）：随创建请求一起提交（Stage 23） -->
          <label
            v-if="kbFormMode === 'create'"
            class="field form-grid__full"
          >
            <span class="field__label">首份文档（可选）</span>
            <input
              class="file-input--inline"
              type="file"
              :accept="ACCEPTED_FILE_EXTENSIONS.join(',')"
              @change="onKbFileSelected"
            />
            <span class="muted">
              支持 {{ ACCEPTED_FILE_EXTENSIONS.join(' / ') }}，单个文件不超过
              {{ MAX_UPLOAD_FILE_SIZE_MB }}MB。文件在建库请求内解析为文档正文，
              原始文件不会被保留；导入后文档为「待索引」，需再触发索引。
            </span>
            <span v-if="kbFormFileNotice" class="muted">
              {{ kbFormFileNotice }}
            </span>
          </label>
          <div class="form-grid__full panel__actions">
            <button
              class="btn btn--primary"
              type="submit"
              :disabled="store.submitting || store.uploading"
            >
              {{ submitKbLabel }}
            </button>
            <button
              class="btn btn--ghost"
              type="button"
              :disabled="store.submitting || store.uploading"
              @click="closeKbForm"
            >
              取消
            </button>
          </div>
        </form>
      </div>

      <!-- 列表视图 -->
      <template v-if="!isDetailMode">
        <div
          v-if="store.loading && store.knowledgeBases.length === 0"
          class="state"
        >
          <span>加载中…</span>
        </div>

        <div v-else-if="store.isEmpty" class="state">
          <span class="state__title">还没有知识库</span>
          <span>点击右上角「+ 新建知识库」创建第一个知识库</span>
        </div>

        <div v-else class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>名称</th>
                <th>描述</th>
                <th>更新时间</th>
                <th class="table__col-actions">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in store.knowledgeBases" :key="item.id">
                <td>
                  <span class="table__strong">{{ item.name }}</span>
                </td>
                <td class="table__muted">{{ item.description || '—' }}</td>
                <td class="table__muted">{{ formatTime(item.updated_at) }}</td>
                <td class="table__actions">
                  <button class="btn btn--sm" type="button" @click="openDetail(item)">
                    查看
                  </button>
                  <button
                    class="btn btn--sm"
                    type="button"
                    :disabled="store.submitting"
                    @click="openEditKb(item)"
                  >
                    编辑
                  </button>
                  <button
                    class="btn btn--danger btn--sm"
                    type="button"
                    :disabled="store.submitting"
                    @click="onDeleteKb(item)"
                  >
                    删除
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- 详情视图 -->
      <template v-else>
        <div v-if="currentKb === null" class="state">
          <span class="state__title">知识库不存在</span>
          <span>它可能已被删除，请返回知识库列表</span>
        </div>

        <template v-else>
          <!-- 知识库信息 -->
          <div class="panel">
            <div class="panel__header">
              <span class="panel__title">{{ currentKb.name }}</span>
              <button
                class="btn btn--sm"
                type="button"
                :disabled="store.submitting"
                @click="openEditKb(currentKb)"
              >
                编辑知识库
              </button>
            </div>
            <div class="panel__body">
              <p class="muted">{{ currentKb.description || '（无描述）' }}</p>
              <p class="muted">
                创建：{{ formatTime(currentKb.created_at) }} ｜ 更新：{{
                  formatTime(currentKb.updated_at)
                }}
              </p>
            </div>
          </div>

          <!-- 索引结果提示（真实状态，不做乐观假设） -->
          <p v-if="indexNotice" class="panel-alert">
            <span
              class="alert"
              :class="indexNotice.ok ? 'alert--info' : 'alert--error'"
            >
              {{ indexNotice.message }}
            </span>
          </p>

          <!-- 文档正文查看 -->
          <div v-if="viewingDoc" class="panel">
            <div class="panel__header">
              <span class="panel__title">
                {{ viewingDoc.name }}（{{ indexStatusLabel(viewingDoc.index_status) }}）
              </span>
              <button
                class="btn btn--ghost btn--sm"
                type="button"
                @click="viewingDoc = null"
              >
                关闭
              </button>
            </div>
            <div class="panel__body">
              <pre class="doc-content">{{ viewingDoc.content }}</pre>
            </div>
          </div>

          <!-- 文档表单（新建 / 编辑） -->
          <div v-if="isDocFormOpen" class="panel">
            <div class="panel__header">
              <span class="panel__title">{{ docFormTitle }}</span>
              <button
                class="btn btn--ghost btn--sm"
                type="button"
                :disabled="store.submitting"
                @click="closeDocForm"
              >
                关闭
              </button>
            </div>
            <form class="panel__body form-grid" @submit.prevent="onSubmitDoc">
              <p v-if="docFormError" class="form-grid__full panel-alert">
                <span class="alert alert--error">{{ docFormError }}</span>
              </p>
              <label class="field form-grid__full">
                <span class="field__label">文档名称 *</span>
                <input
                  v-model="docForm.name"
                  class="input"
                  type="text"
                  maxlength="200"
                  placeholder="例如：售后政策.md"
                />
              </label>
              <label class="field form-grid__full">
                <span class="field__label">文档正文 *</span>
                <textarea
                  v-model="docForm.content"
                  class="textarea"
                  rows="8"
                  placeholder="粘贴或输入文档正文（索引时由后端 RAG 引擎切分）"
                />
              </label>
              <div class="form-grid__full panel__actions">
                <button
                  class="btn btn--primary"
                  type="submit"
                  :disabled="store.submitting"
                >
                  {{ store.submitting ? '保存中…' : '保存' }}
                </button>
                <button
                  class="btn btn--ghost"
                  type="button"
                  :disabled="store.submitting"
                  @click="closeDocForm"
                >
                  取消
                </button>
              </div>
            </form>
          </div>

          <!-- 上传文件（Stage 22：解析为纯文本后原始文件即被删除） -->
          <div class="panel">
            <div class="panel__header">
              <span class="panel__title">上传文件</span>
            </div>
            <div class="panel__body">
              <p class="muted">
                支持 {{ ACCEPTED_FILE_EXTENSIONS.join(' / ') }}，单个文件不超过
                {{ MAX_UPLOAD_FILE_SIZE_MB }}MB。后端只保存解析出的文本，
                <strong>原始文件不会被保留</strong>。
              </p>
              <p class="muted">
                上传后文档状态为「待索引」，需点击该文档的「重新索引」才会写入向量库。
              </p>
              <div class="panel__actions">
                <button
                  class="btn btn--primary btn--sm"
                  type="button"
                  :disabled="store.uploading || store.submitting"
                  @click="openFilePicker"
                >
                  {{ store.uploading ? '上传中…' : '选择文件并上传' }}
                </button>
                <input
                  ref="fileInput"
                  class="file-input"
                  type="file"
                  :accept="ACCEPTED_FILE_EXTENSIONS.join(',')"
                  @change="onFileSelected"
                />
              </div>
              <p v-if="uploadNotice" class="panel-alert">
                <span
                  class="alert"
                  :class="uploadNotice.ok ? 'alert--info' : 'alert--error'"
                >
                  {{ uploadNotice.message }}
                </span>
              </p>
            </div>
          </div>

          <!-- 文档列表 -->
          <div class="section-bar">
            <span class="section-bar__title">文档</span>
            <button
              class="btn btn--primary btn--sm"
              type="button"
              :disabled="store.submitting || isDocFormOpen"
              @click="openCreateDoc"
            >
              + 新建文档
            </button>
          </div>

          <div
            v-if="store.documentsLoading && store.documents.length === 0"
            class="state"
          >
            <span>加载中…</span>
          </div>

          <div v-else-if="store.documentsEmpty" class="state">
            <span class="state__title">还没有文档</span>
            <span>点击「+ 新建文档」添加第一份原始文档，然后触发索引</span>
          </div>

          <div v-else class="table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th>文档名称</th>
                  <th>索引状态</th>
                  <th>更新时间</th>
                  <th class="table__col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="doc in store.documents" :key="doc.id">
                  <td>
                    <span class="table__strong">{{ doc.name }}</span>
                  </td>
                  <td>
                    <span
                      class="badge"
                      :class="indexStatusClass(doc.index_status)"
                    >
                      {{ indexStatusLabel(doc.index_status) }}
                    </span>
                  </td>
                  <td class="table__muted">{{ formatTime(doc.updated_at) }}</td>
                  <td class="table__actions">
                    <button
                      class="btn btn--sm"
                      type="button"
                      @click="onViewDoc(doc)"
                    >
                      查看
                    </button>
                    <button
                      class="btn btn--sm"
                      type="button"
                      :disabled="store.submitting"
                      @click="openEditDoc(doc)"
                    >
                      编辑
                    </button>
                    <button
                      class="btn btn--sm"
                      type="button"
                      :disabled="store.submitting"
                      @click="onIndexDoc(doc)"
                    >
                      重新索引
                    </button>
                    <button
                      class="btn btn--danger btn--sm"
                      type="button"
                      :disabled="store.submitting"
                      @click="onDeleteDoc(doc)"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>
    </div>



  </section>
</template>

