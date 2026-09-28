# Enterprise AI Agent Platform

企业级 AI Agent 平台（Backend: FastAPI + PostgreSQL；Frontend: Vue 3 + Vite）。

## 开发端口约定

本项目的端口与另一个项目 **Travel-H5** 严格区分，两个项目可以同时在本机运行：

| 项目 | 前端 | 后端 |
| --- | --- | --- |
| **Enterprise-AI-Agent-Platform（本项目）** | `http://localhost:5174` | `http://localhost:8001` |
| Travel-H5（本项目不修改） | 5173 | 8000 |

前端 dev server 端口已在 `frontend/vite.config.ts` 中固定为 `5174` 并设置 `strictPort: true`：
5174 被占用时启动会直接失败，**不会**自动切换到 5175。

## 启动后端

```powershell
cd backend
uv run uvicorn app.main:app --reload --port 8001
```

- 根路径：`GET http://localhost:8001/` → `{"message": "Enterprise AI Agent Platform API"}`
- 接口文档：`http://localhost:8001/docs`
- 配置来自 `backend/.env`（可参考 `backend/.env.example`）
- 数据库迁移：`uv run alembic upgrade head`

## 启动前端

```powershell
cd frontend
npm run dev
```

- 访问地址：`http://localhost:5174`
- API 地址来自 `frontend/.env` 的 `VITE_API_BASE_URL`（可参考 `frontend/.env.example`），当前为 `http://localhost:8001`

## 完整链路

```text
Browser
   ↓  http://localhost:5174        （Vite dev server）
Axios    frontend/src/utils/request.ts
   ↓  http://localhost:8001        （FastAPI）
Backend
```

开发环境下后端 CORS 仅允许 `http://localhost:5174`，见 `backend/app/middleware/cors.py`。

## Agent 配置 → Chat 执行（Stage 17）

管理员在「Agent 管理」页面配置的 Agent 参数会真正进入用户对话链路：

```text
Admin：Agent 管理（provider / model / temperature / system_prompt / 知识库 / 工具）
   ↓  agents / agent_tools（Alembic 007）
用户：AI 工作台选择 Agent
   ↓  POST /agent/chat { conversation_id, agent_id, message }
AgentService：读取 Agent 配置 → RAG（该 Agent 绑定的知识库 collection）→ 工具子集
   ↓
AgentRuntime → LLMProvider（qwen / ollama）→ LLM
```

- 请求不携带 `agent_id` 时沿用 `backend/.env` 的全局默认配置
  （provider / model / temperature / 全部内置工具），且**不执行 RAG**（Stage 20 起）；
- `agents.knowledge_base_id` 为 `ON DELETE SET NULL`：删除知识库后该 Agent
  自动退化为「无知识库」正常对话，不会出现悬空引用导致的 500；
- 工具绑定只能选择后端已注册的工具（`GET /agents/tools`），
  不接受任何管理员自定义代码；
- 权限：普通用户只可 `GET /agents/available`（选择器）+ 使用 Agent 对话；
  Agent / 知识库管理接口全部要求管理员（未登录 401、普通用户 403）。

## 平台概览 Dashboard（Stage 18）

`/app/admin`（**仅管理员**：前端路由 `meta.requiresAdmin` + 后端 `require_admin` 双重保护）
展示平台核心资源的真实数量：

```text
用户 / Agent / 知识库 / 文档 / 会话 / 消息
   ↑
GET /admin/dashboard（管理员专属）
   ↓
AdminService.get_dashboard_stats() → Repository → PostgreSQL（实时 COUNT(*)）
```

- 不缓存、不估算、**不显示任何无法可靠获取的指标**（无 CPU / QPS / 假健康检查）；
- 数据流保持项目分层：Router → Service → Repository → DB（Router 不直接查库）。

## 管理员用户管理（Stage 19）

`/app/admin` 页面在平台概览下方提供**用户管理**区域（仅管理员可见可操作）：

```text
AdminView.vue（用户管理面板）
   ↓  stores/admin.ts（users / total / page / loading / error）
   ↓  api/admin.ts：fetchAdminUsers / updateAdminUserRole / deleteAdminUser
   ↓
GET  /admin/users?page=&page_size=      → 分页列表（后端分页，前端不本地分页）
PATCH /admin/users/{id}/role {role}     → user ⇄ admin
DELETE /admin/users/{id}                → 204
   ↓
require_admin（401 / 403）→ AdminService → UserRepository → PostgreSQL
```

- **权限双层**：前端 `meta.requiresAdmin` + 菜单隐藏（体验层），后端 `require_admin`（真实边界）；
- **后端既有规则原样遵循**：管理员不能修改自己的角色、不能删除自己（前端仅禁用按钮并提示，后端返回 403）；
- **写操作后重新拉取当前页**，保证前端状态与服务器一致；失败时同样回拉并按后端消息提示（不显示假成功）；
- 删除用户前必须二次确认（`window.confirm`）；提示会影响用户总数 → 同步刷新 Dashboard 统计；
- 未引入任何新依赖、未新增 migration、未改动 Dashboard 与既有权限体系。

> 提示：只有在**存在 admin 账号**时才可进入 `/app/admin`。若当前库中没有管理员，
> 可执行（把用户名替换成你自己的账号）：
>
> ```sql
> UPDATE users SET role = 'admin' WHERE username = '你的用户名';
> ```

### Stage 19 验收脚本

```powershell
cd backend
$env:PYTHONPATH = (Get-Location).Path
uv run python scripts/admin_user_management_acceptance_test.py
```

覆盖：Admin 列表与数据库交叉校验、角色双向修改、删除用户、普通用户 403、
未登录 401、自改/自删 403、非法角色 422、目标不存在 404、后端分页，
以及**前端接线静态检查**（路由 `requiresAdmin`、Sidebar、api/store/View 引用、登出重置）。

## RAG 知识来源（Stage 20）

项目**不再预置任何默认 RAG 知识数据**；正式 RAG 知识只有一个入口：管理员经
Knowledge Base 管理。

```text
管理员（/app/knowledge）
   ↓ 创建 Knowledge Base → 添加 Document（name + content）→ Index
KnowledgeBaseService._rebuild_index()
   ↓ TextSplitter → Embedding → VectorStore
Chroma collection = {VECTOR_COLLECTION_NAME}__kb_{knowledge_base_id}
```

| 场景 | Provider / Model / Temperature | 工具 | RAG |
|---|---|---|---|
| 平台默认对话（`agent_id` 省略） | `backend/.env` 全局配置（Qwen / `LLM_MODEL` / `LLM_TEMPERATURE`） | 全部内置工具（calculator、datetime） | **不执行**（不绑定任何知识库） |
| Managed Agent（`agent_id` 指定） | 该 Agent 的配置 | 该 Agent 绑定的工具子集 | 仅当 `knowledge_base_id` 非空时，检索该知识库的 collection |

- `backend/knowledge/*.txt` 与 `scripts/verify_rag_context.py` 已在 Stage 20 删除
  （它们服务于「预置默认 RAG」这一已移除的链路）；
- `app/rag/*` 全部保留（Loader / Splitter / Embeddings / VectorStore / Retriever /
  Exceptions），`build_index.py` 保留为**离线自检 CLI**（不在运行时链路中）；
- `backend/.chroma/` 是本地向量缓存（已被 `.gitignore` 忽略）：为空或不存在时，
  首次 Index 会自动重建；删除 Knowledge Base 时其向量被清空；
- 前端**不直接访问** `.chroma`：只能通过 Knowledge Base / Document / Index API。

### Stage 20 验收脚本

```powershell
cd backend
$env:PYTHONPATH = (Get-Location).Path
uv run python scripts/default_rag_removal_acceptance_test.py
```

覆盖：启动不加载默认 RAG、默认对话不使用 RAG（且默认参数与工具仍生效）、
KB → Document → Index → Chroma、Managed Agent + KB 的 RAG、无默认 collection
时对话仍正常、旧默认数据已不存在。

## 权限与验收脚本（Stage 18）

```powershell
cd backend
$env:PYTHONPATH = (Get-Location).Path     # 脚本需要以 backend 为根导入 app

# 1) 全量权限审计（guest / user / admin 矩阵 + ownership 隔离 + 越权不泄露内容）
uv run python scripts/permission_audit_test.py

# 2) 最终端到端验收（Admin → KB → 文档 → 索引 → Agent → Tool → User → Chat → RAG → LLM）
uv run python scripts/final_acceptance_test.py
```

两个脚本都会**自动清理**自己创建的测试数据（用户 / Agent / 知识库 / 文档 / 会话），
且不会读取或打印任何密钥；最终验收在检测到 `LLM_API_KEY` 与后端
`http://127.0.0.1:8001` 可用时会追加一次**真实 LLM 真实 HTTP** 调用（否则明确 SKIP，不伪造）。

权限收紧说明（Stage 18 审计修正）：`GET /users/{id}` 此前**没有认证依赖**，
现在要求登录且**只能查看自己**（他人 403）；管理员查看任意用户请使用
`GET /admin/users/{id}`。
