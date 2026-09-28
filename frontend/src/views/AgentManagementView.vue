<script setup lang="ts">
/**
 * Agent 管理页面（Stage 15 / Stage 17）。
 *
 * 位置
 * ----
 * 平台外壳（``layouts/MainLayout.vue``）内的 ``/app/agents`` 模块页面。
 * 仅管理员可进入（见 ``router/index.ts`` 的 ``meta.requiresAdmin``）。
 *
 * 职责
 * ----
 * 管理 **Agent 配置**：名称 / 描述 / 系统提示词 / Provider / Model /
 * Temperature / **绑定的知识库** / **允许使用的工具**（Stage 17）。
 * 支持列表、创建、编辑（含详情）、删除。
 *
 * 这些配置会真正影响用户对话（``POST /agent/chat`` 携带 ``agent_id``）：
 *
 *     provider / model / temperature → 该次对话使用的 Provider
 *     system_prompt                  → system 消息（与 RAG context 合并为一条）
 *     knowledge_base_id              → RAG 检索（NULL = 不检索）
 *     tool_names                     → 只向 LLM 暴露这些已注册工具
 *
 * 数据流
 * ------
 *     本页面 → stores/agent.ts → api/agent.ts（/agents/*）
 *              ↓（知识库下拉）      → api/knowledgeBase.ts（/knowledge-bases，只读列表）
 *              → FastAPI → AgentService / Repository
 *
 * 权限
 * ----
 * 前端：路由守卫 + 菜单隐藏（体验层）；
 * 后端：``/agents`` 管理接口全部要求 admin（普通用户 403）—— 前端不能替代后端。
 *
 * 说明：
 * - Provider 下拉选项只是 UI 便利（与后端 ``SUPPORTED_PROVIDERS`` 对应）；
 * - 工具选项来自后端 ``GET /agents/tools``（ToolRegistry 单一来源），
 *   合法性最终由后端校验（非法值返回 400，本页面直接展示该错误文案）。
 */

import { computed, onMounted, reactive, ref } from 'vue'

import type { AgentConfig } from '../api/agent'
import { listKnowledgeBases, type KnowledgeBase } from '../api/knowledgeBase'
import { useAgentStore } from '../stores/agent'

const agents = useAgentStore()

/** Provider 可选项（UI 便利；权威校验在后端 SUPPORTED_PROVIDERS） */
const PROVIDER_OPTIONS = ['qwen', 'ollama'] as const

/** 温度取值范围（与后端 AgentCreate.temperature 的 ge/le 一致） */
const TEMPERATURE_MIN = 0
const TEMPERATURE_MAX = 2

/** 知识库列表（表单下拉用；加载失败只影响该下拉，不阻塞 Agent 管理） */
const knowledgeBases = ref<KnowledgeBase[]>([])

/** 表单模式：null = 未打开；create = 新建；edit = 编辑 */
const formMode = ref<'create' | 'edit' | null>(null)
/** 编辑中的 Agent ID（仅 edit 模式有值） */
const editingId = ref<number | null>(null)
/** 表单错误文案（本地校验或后端归一化错误） */
const formError = ref<string | null>(null)

/** 表单数据（字段与后端 AgentCreate / AgentUpdate 对齐） */
const form = reactive({
  name: '',
  description: '',
  system_prompt: '',
  provider: PROVIDER_OPTIONS[0] as string,
  model: '',
  temperature: 0.7,
  knowledge_base_id: null as number | null,
  tool_names: [] as string[],
})

/** 工具多选项：注册表工具 + 已绑定但不在注册表返回列表中的工具（避免静默丢失） */
const formToolOptions = computed(() => {
  const known = agents.toolOptions.map((item) => item.name)
  const extra = form.tool_names
    .filter((name) => !known.includes(name))
    .map((name) => ({ name, description: '（后端未返回该工具的说明）' }))
  return [...agents.toolOptions, ...extra]
})

const isFormOpen = computed(() => formMode.value !== null)
const formTitle = computed(() =>
  formMode.value === 'edit' ? '编辑 Agent' : '新建 Agent',
)

onMounted(() => {
  void agents.load()
  // 工具选项（ToolRegistry 单一来源）与知识库下拉（只读）
  void agents.loadToolOptions()
  void loadKnowledgeBases()
})

/** 加载知识库列表（仅用于表单下拉；失败时下拉为空，不影响其他功能） */
async function loadKnowledgeBases(): Promise<void> {
  try {
    knowledgeBases.value = await listKnowledgeBases()
  } catch {
    knowledgeBases.value = []
  }
}

/** 切换某个工具的绑定状态 */
function toggleTool(name: string): void {
  if (form.tool_names.includes(name)) {
    form.tool_names = form.tool_names.filter((item) => item !== name)
    return
  }
  form.tool_names = [...form.tool_names, name]
}

/** 列表展示：知识库 ID → 名称（找不到时回退为 #ID） */
function knowledgeBaseName(knowledgeBaseId: number | null): string {
  if (knowledgeBaseId === null) {
    return '—'
  }
  const found = knowledgeBases.value.find(
    (item) => item.id === knowledgeBaseId,
  )
  return found ? found.name : `#${knowledgeBaseId}`
}

/** 重置表单内容（不改变打开状态） */
function resetForm(): void {
  form.name = ''
  form.description = ''
  form.system_prompt = ''
  form.provider = PROVIDER_OPTIONS[0]
  form.model = ''
  form.temperature = 0.7
  form.knowledge_base_id = null
  form.tool_names = []
  formError.value = null
}

/** 打开「新建」表单 */
function openCreate(): void {
  formMode.value = 'create'
  editingId.value = null
  resetForm()
}

/** 打开「编辑」表单：先读取最新配置（GET /agents/{id}），再填充表单 */
async function openEdit(agent: AgentConfig): Promise<void> {
  formMode.value = 'edit'
  editingId.value = agent.id
  resetForm()

  const latest = await agents.fetchOne(agent.id)
  if (latest === null) {
    formError.value = agents.error ?? '加载 Agent 配置失败'
  }
  const source = latest ?? agent
  form.name = source.name
  form.description = source.description
  form.system_prompt = source.system_prompt
  form.provider = source.provider
  form.model = source.model
  form.temperature = source.temperature
  form.knowledge_base_id = source.knowledge_base_id
  form.tool_names = [...source.tool_names]
}

/** 关闭表单 */
function closeForm(): void {
  formMode.value = null
  editingId.value = null
  formError.value = null
}

/** 提交前本地校验（后端仍会独立校验，这里只是快速反馈） */
function validate(): string | null {
  if (form.name.trim() === '') {
    return '请填写 Agent 名称'
  }
  if (form.provider.trim() === '') {
    return '请选择 Provider'
  }
  if (form.model.trim() === '') {
    return '请填写模型名称'
  }
  const temperature = Number(form.temperature)
  if (
    Number.isNaN(temperature) ||
    temperature < TEMPERATURE_MIN ||
    temperature > TEMPERATURE_MAX
  ) {
    return `Temperature 必须在 ${TEMPERATURE_MIN} - ${TEMPERATURE_MAX} 之间`
  }
  return null
}

/** 提交表单（新建 / 编辑）；成功后关闭表单（store 内部会刷新列表） */
async function onSubmit(): Promise<void> {
  if (agents.submitting) {
    return
  }
  const validationError = validate()
  if (validationError !== null) {
    formError.value = validationError
    return
  }
  formError.value = null

  const payload = {
    name: form.name.trim(),
    description: form.description.trim(),
    system_prompt: form.system_prompt.trim(),
    provider: form.provider.trim(),
    model: form.model.trim(),
    temperature: Number(form.temperature),
    // null = 不使用 RAG；后端会校验该知识库是否存在（不存在返回 404）
    knowledge_base_id: form.knowledge_base_id,
    // 覆盖式设置：只向 LLM 暴露这些已注册工具（未注册名称后端返回 400）
    tool_names: [...form.tool_names],
  }

  const result =
    editingId.value === null
      ? await agents.create(payload)
      : await agents.update(editingId.value, payload)

  if (result !== null) {
    closeForm()
  } else {
    formError.value = agents.error ?? '保存失败，请重试'
  }
}

/** 删除 Agent（必须二次确认；不直接删除） */
async function onDelete(agent: AgentConfig): Promise<void> {
  const confirmed = window.confirm(
    `确认删除 Agent「${agent.name}」？该操作不可撤销。`,
  )
  if (!confirmed) {
    return
  }
  await agents.remove(agent.id)
  if (editingId.value === agent.id) {
    closeForm()
  }
}

/** 时间格式化（列表展示） */
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
</script>

<template>
  <section class="module-page">
    <header class="module-page__header">
      <div>
        <h2 class="module-page__title">Agent 管理</h2>
        <p class="module-page__subtitle">管理企业内部 AI Agent 的配置</p>
      </div>
      <button
        class="btn btn--primary btn--sm"
        type="button"
        :disabled="agents.submitting || isFormOpen"
        @click="openCreate"
      >
        + 新建 Agent
      </button>
    </header>

    <div class="module-page__body module-page__body--top">
      <p v-if="agents.error" class="panel-alert">
        <span class="alert alert--error">{{ agents.error }}</span>
      </p>

      <!-- 创建 / 编辑表单（内联面板；本阶段不引入 Modal 组件与额外路由） -->
      <div v-if="isFormOpen" class="panel">
        <div class="panel__header">
          <span class="panel__title">{{ formTitle }}</span>
          <button
            class="btn btn--ghost btn--sm"
            type="button"
            :disabled="agents.submitting"
            @click="closeForm"
          >
            关闭
          </button>
        </div>

        <form class="panel__body form-grid" @submit.prevent="onSubmit">
          <p v-if="formError" class="form-grid__full panel-alert">
            <span class="alert alert--error">{{ formError }}</span>
          </p>

          <label class="field">
            <span class="field__label">名称 *</span>
            <input
              v-model="form.name"
              class="input"
              type="text"
              maxlength="100"
              placeholder="例如：客服助手"
            />
          </label>

          <label class="field">
            <span class="field__label">Provider *</span>
            <select v-model="form.provider" class="input">
              <option v-for="option in PROVIDER_OPTIONS" :key="option" :value="option">
                {{ option }}
              </option>
            </select>
          </label>

          <label class="field">
            <span class="field__label">Model *</span>
            <input
              v-model="form.model"
              class="input"
              type="text"
              maxlength="100"
              placeholder="例如：qwen3-8b"
            />
          </label>

          <label class="field">
            <span class="field__label">Temperature（0 - 2）</span>
            <input
              v-model.number="form.temperature"
              class="input"
              type="number"
              min="0"
              max="2"
              step="0.1"
            />
          </label>

          <label class="field">
            <span class="field__label">知识库（可选，用于 RAG）</span>
            <select v-model="form.knowledge_base_id" class="input">
              <option :value="null">不使用知识库</option>
              <option
                v-for="item in knowledgeBases"
                :key="item.id"
                :value="item.id"
              >
                {{ item.name }}
              </option>
            </select>
          </label>

          <div class="field form-grid__full">
            <span class="field__label">允许使用的工具</span>
            <div class="checkbox-group">
              <label
                v-for="tool in formToolOptions"
                :key="tool.name"
                class="checkbox-group__item"
                :title="tool.description"
              >
                <input
                  type="checkbox"
                  :checked="form.tool_names.includes(tool.name)"
                  :disabled="agents.submitting"
                  @change="toggleTool(tool.name)"
                />
                <span>{{ tool.name }}</span>
              </label>
              <span v-if="formToolOptions.length === 0" class="field__hint">
                暂无可用工具
              </span>
            </div>
            <span class="field__hint">
              只暴露勾选的工具给 LLM；不勾选则纯文本对话（工具均为后端已注册工具，不接受任意代码）
            </span>
          </div>

          <label class="field form-grid__full">
            <span class="field__label">描述</span>
            <input
              v-model="form.description"
              class="input"
              type="text"
              maxlength="500"
              placeholder="例如：负责处理企业售后问题"
            />
          </label>

          <label class="field form-grid__full">
            <span class="field__label">System Prompt</span>
            <textarea
              v-model="form.system_prompt"
              class="textarea"
              rows="4"
              maxlength="4000"
              placeholder="例如：你是一名企业售后客服助手，请用简洁专业的中文回答用户问题。"
            />
          </label>

          <div class="form-grid__full panel__actions">
            <button
              class="btn btn--primary"
              type="submit"
              :disabled="agents.submitting"
            >
              {{ agents.submitting ? '保存中…' : '保存' }}
            </button>
            <button
              class="btn btn--ghost"
              type="button"
              :disabled="agents.submitting"
              @click="closeForm"
            >
              取消
            </button>
          </div>
        </form>
      </div>

      <!-- 列表：loading / empty / 数据 -->
      <div v-if="agents.loading && agents.items.length === 0" class="state">
        <span>加载中…</span>
      </div>

      <div v-else-if="agents.isEmpty" class="state">
        <span class="state__title">还没有 Agent</span>
        <span>点击右上角「+ 新建 Agent」创建第一个 Agent 配置</span>
      </div>

      <div v-else class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Temperature</th>
              <th>知识库</th>
              <th>工具</th>
              <th>描述</th>
              <th>更新时间</th>
              <th class="table__col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="agent in agents.items" :key="agent.id">
              <td><span class="table__strong">{{ agent.name }}</span></td>
              <td><span class="badge">{{ agent.provider }}</span></td>
              <td>{{ agent.model }}</td>
              <td>{{ agent.temperature }}</td>
              <td class="table__muted">
                {{ knowledgeBaseName(agent.knowledge_base_id) }}
              </td>
              <td class="table__muted">
                <template v-if="agent.tool_names.length > 0">
                  <span
                    v-for="name in agent.tool_names"
                    :key="name"
                    class="badge"
                  >
                    {{ name }}
                  </span>
                </template>
                <template v-else>—</template>
              </td>
              <td class="table__muted">{{ agent.description || '—' }}</td>
              <td class="table__muted">{{ formatTime(agent.updated_at) }}</td>
              <td class="table__actions">
                <button
                  class="btn btn--sm"
                  type="button"
                  :disabled="agents.submitting"
                  @click="openEdit(agent)"
                >
                  编辑
                </button>
                <button
                  class="btn btn--danger btn--sm"
                  type="button"
                  :disabled="agents.submitting"
                  @click="onDelete(agent)"
                >
                  删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

  </section>
</template>

