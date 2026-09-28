# Enterprise AI Agent Platform

一个基于 **FastAPI + Vue 3** 构建的企业级 AI Agent 平台：支持多轮对话、RAG 知识库、工具调用与多模型 Provider 抽象。

- **完整业务闭环**：用户与 Agent 对话 → Agent 检索企业私有知识库 → 调用受控工具 → 返回答案，全过程持久化、可观测。
- **分层清晰**：Router（薄层）→ Service（业务）→ Repository（数据）→ PostgreSQL；Agent Runtime 与 LLM Provider 解耦，Provider 可替换（Qwen / Ollama）。
- **RAG 全链路**：知识库管理 → 文件上传（TXT / Markdown / PDF / DOCX）→ 文本切分 → Embedding → ChromaDB → 检索增强上下文。
- **工程化约束**：统一错误响应、唯一事务边界、真实统计（不缓存、不估算、不谎报索引成功）、管理员权限双层校验。

> **Tech Stack**：Python 3.14 · FastAPI · SQLAlchemy Async · PostgreSQL · Alembic · ChromaDB ｜ Vue 3 · TypeScript · Vite · Pinia · Axios

---

## Features

### Authentication

- **用户注册**：`POST /users/`，用户名与邮箱全局唯一
- **JWT 登录**：`POST /auth/login` 使用邮箱 + 密码登录，签发 HS256 Bearer Token（有效期默认 30 分钟）
- **bcrypt 密码加密**：密码仅以 bcrypt 哈希存储（passlib），任何接口都不返回 `password` 字段
- **身份识别**：`GET /users/me` 返回当前登录用户
- **用户自助**：`GET / PATCH / DELETE /users/{id}` 只允许操作**本人**账号（他人 403）
- **角色与权限**：`user` / `admin` 两种角色；管理接口由后端 `require_admin` 强制（未登录 **401**、普通用户 **403**）
- **Admin 管理能力**：分页用户列表、查看任意用户、角色升降级、删除用户；管理员**不能**修改自己的角色、**不能**删除自己
- **防用户名枚举**：用户不存在与密码错误返回完全相同的 401 消息，不泄露账号是否存在

### Conversation

- **会话持久化**：`conversations` 表保存标题、归属用户与时间戳
- **会话 CRUD**：创建、分页列表（`page` / `page_size`）、详情、重命名、删除（204）
- **消息历史**：`GET /conversations/{id}/messages` 按时间正序返回 user / assistant 消息，用于刷新页面或切换会话时恢复聊天记录
- **消息只读**：消息只由 `POST /agent/chat` 产生，不提供外部写入入口
- **归属隔离**：只能访问自己的会话（不存在 404、属于他人 403）
- **多轮上下文对话**：同一会话的历史消息随请求一起交给 LLM，支持连续追问

### Agent Runtime

- **框架无关**：`AgentRuntime` 不依赖 FastAPI / 数据库 / Repository，也不感知 RAG 与向量库，可独立测试
- **Provider 解耦**：Runtime 只依赖 `LLMProvider` 抽象接口，具体厂商实现通过工厂注入
- **Provider 工厂模式**：`get_llm_provider()` 支持 `qwen`（OpenAI 兼容协议，默认）与 `ollama`；Provider 名称非法时**明确报错**，不静默回退
- **Tool Calling 循环**：LLM 返回 `tool_calls` → Runtime 执行工具 → 结果回传 LLM → 直到得到最终回答；循环有明确上界（默认 3 轮，达上限后必定返回最终回答）
- **执行过程可观测**：一次执行被表达为 `AgentExecution`（messages / iterations / tool_calls / tool_results / final_response / stop_reason），并通过 Observer 钩子（start / tool_call / finish）记录日志
- **错误边界**：工具错误以 `Tool error: ...` 作为工具结果回传 LLM（仍返回 200，不升级为 500）；Provider 网络 / 协议错误在 API 边界统一转换为 502
- **空响应兜底**：模型返回空内容时给出明确文案，避免写入空消息

### Managed Agent（Agent 配置管理）

- **管理员配置**：`/agents` 提供 Agent CRUD —— 名称、描述、系统提示词、provider、model、temperature、知识库绑定、工具绑定
- **普通用户视角**：`GET /agents/available` 只返回 id / name / description，用于工作台选择器，不暴露 `system_prompt` 等管理配置
- **对话时生效**：`POST /agent/chat` 携带 `agent_id` 时，使用该 Agent 的 Provider、模型、温度、系统提示词、知识库与工具子集；不携带时沿用平台默认配置（`.env`）且不执行 RAG
- **引用安全**：`agents.knowledge_base_id` 为 `ON DELETE SET NULL`，删除知识库后 Agent 自动退化为「无知识库」正常对话，不会出现悬空引用导致的 500
- **工具绑定受控**：`tool_names` 只能取后端已注册工具（`GET /agents/tools`），不接受任何自定义代码

### RAG Knowledge Base

- **知识库管理**：`/knowledge-bases` 管理员接口，创建 / 列表 / 详情 / 更新 / 删除（删除时级联文档并清理该知识库的向量 collection）
- **文档来源**：直接输入正文（JSON）或上传文件（`multipart/form-data`）
- **文件上传**：支持 `.txt` / `.md` / `.pdf`（pypdf）/ `.docx`（OOXML），单文件上限 `MAX_UPLOAD_FILE_SIZE_MB`（默认 10MB）
- **不保存原始文件**：文件在临时目录解析为纯文本后立即删除，数据库只保留提取出的正文
- **文本切分**：`TextSplitter`（`CHUNK_SIZE` 默认 500、`CHUNK_OVERLAP` 默认 50）
- **Embedding**：可切换 `hash`（本地确定性散列，用于链路自检）/ `ollama` / `dashscope`
- **ChromaDB 向量存储**：持久化到 `VECTOR_STORE_DIR`（默认 `.chroma`），每个知识库一个独立 collection（`{VECTOR_COLLECTION_NAME}__kb_{知识库 ID}`），实现多知识库隔离；删除知识库时同时清理 collection 元数据与物理目录
- **索引与状态**：`POST /knowledge-bases/{id}/documents/{doc_id}/index` 重建该知识库索引并返回真实 `status`（`indexed` / `failed`），**绝不谎报成功**；文档 `index_status` 持久化为 `pending` / `indexed` / `failed`；修改正文会重置为 `pending`
- **Retrieval 增强上下文**：`Retriever` 按 query 向量检索 Top-K（`RAG_TOP_K` 默认 4）片段并注入 Agent 上下文；检索失败静默降级，不影响正常对话
- **创建即导入（可选）**：`POST /knowledge-bases` 支持 multipart 表单，可在建库的同一个请求、同一个事务内导入首份文档；任一步失败整体回滚，不留半成品

### Tool Calling

- **Tool 抽象接口**：统一 `name` / `description` / `parameters`（JSON Schema）/ `execute()`，把外部能力包装成 LLM 可调用的函数
- **Tool Registry**：注册、按名查找、列举、统一参数校验后执行；不硬编码任何具体工具的名称与参数规则，新增工具只需注册一行
- **Schema 驱动校验**：参数缺失 / 类型错误 / 未定义参数 / enum 非法在**执行前**被拒绝
- **内置工具**：`calculator`（四则运算）、`datetime`（当前日期 / 时间）
- **安全执行限制**：Calculator 使用 AST 白名单求值，**不使用 `eval` / `exec`** —— 只允许数字、`+ - * /`、括号与一元正负号；变量、函数调用、属性访问、下标、import、文件 / 网络 / 系统操作一律拒绝；表达式长度上限 200
- **按 Agent 授权**：每个 Agent 只能看到自己绑定的工具子集；`AGENT_TOOL_CALLING_ENABLED` 可整体关闭工具调用，关闭后 Agent 退化为纯文本对话
- **工具名单唯一来源**：`GET /agents/tools` 暴露后端注册表，前端不维护第二份工具名单

### Admin & Dashboard

- **平台概览**：`GET /admin/dashboard` 实时返回用户 / Agent / 知识库 / 文档 / 会话 / 消息数量（数据库实时 `COUNT(*)`，不缓存、不估算、不显示无法可靠获取的指标）
- **用户管理**：`GET /admin/users`（后端分页）、`GET /admin/users/{id}`、`PATCH /admin/users/{id}/role`、`DELETE /admin/users/{id}`
- **前端入口**：`/app/admin` 页面展示概览与用户管理，仅管理员可见可进入

---

## Tech Stack

### Backend

| 技术 | 用途 |
| --- | --- |
| Python 3.14 | 运行环境 |
| FastAPI | HTTP API、依赖注入、自动 OpenAPI 文档 |
| SQLAlchemy Async + asyncpg | 异步 ORM 与 PostgreSQL 驱动 |
| PostgreSQL | 业务数据存储（用户 / 会话 / 消息 / Agent / 知识库 / 文档） |
| Alembic | 数据库版本迁移（`backend/alembic/versions/`） |
| Pydantic v2 / pydantic-settings | 请求响应模型与配置加载 |
| python-jose（JWT）| 访问令牌签发与校验（HS256） |
| passlib + bcrypt | 密码哈希存储 |
| uv | 依赖与虚拟环境管理（`pyproject.toml` + `uv.lock`） |

### AI

| 技术 | 用途 |
| --- | --- |
| Agent Runtime | 执行循环、Tool Calling、多轮上下文组装 |
| LLM Provider 抽象 | 统一 `generate` 接口 + 工厂，运行时与厂商解耦 |
| Qwen（DashScope 兼容端点） | 默认云端 LLM |
| Ollama | 本地 LLM（可选，可完全离线） |
| RAG Pipeline | Loader → Splitter → Embedding → Retriever |
| ChromaDB 1.5 | 向量存储与相似度检索（本地持久化） |
| Embedding | hash（自检）/ ollama / dashscope 三种 Provider |
| Tool Calling | Tool 抽象 + Registry + Schema 参数校验 |
| pypdf / OOXML | PDF、DOCX 文本提取（无外部服务） |

### Frontend

| 技术 | 用途 |
| --- | --- |
| Vue 3（Composition API） | 单页应用 |
| TypeScript | 类型安全 |
| Vite | 开发服务器与构建 |
| Pinia | 状态管理（auth / user / chat / conversation / agent / knowledgeBase / dashboard / admin） |
| Axios | 统一 HTTP 客户端（唯一出口，自动附加 Bearer Token） |
| Vue Router | 路由与登录 / 管理员守卫 |

---

## Architecture

### 分层结构

```text
Browser（Vue 3 SPA）
      │  Axios：frontend/src/utils/request.ts（自动附带 Bearer Token）
      ▼
FastAPI Router          ← 薄层：解析请求 / 鉴权依赖 / 组装响应
      │  Depends（app/core/dependencies.py）
      ▼
Service Layer           ← 业务规则、权限校验、流程编排
      │
      ├──────────────► Repository ──► SQLAlchemy Async ──► PostgreSQL
      │                    （只 flush，不 commit）
      │
      └──────────────► Agent Runtime（不依赖数据库 / FastAPI）
                             │
                             ├──► ToolRegistry ──► Tool（calculator / datetime）
                             └──► LLMProvider（工厂 get_llm_provider）
                                       ├── QwenProvider
                                       └── OllamaProvider
```

### RAG 链路

```text
知识入库
  Document（正文 / 上传文件：txt · md · pdf · docx）
      → Loader        解析为纯文本（上传文件解析后即删除原件）
      → Splitter      按 CHUNK_SIZE / CHUNK_OVERLAP 切分
      → Embedding     EmbeddingProvider（hash / ollama / dashscope）
      → VectorStore   ChromaDB，collection = {VECTOR_COLLECTION_NAME}__kb_{kb_id}

对话检索
  User Query → Embedding → VectorStore.query(top_k) → RetrievedChunk[]
      → Retrieved Context（+ 会话历史 + Agent 系统提示词）
      → Agent Runtime → LLM Provider → 回答
```

### 一次对话请求

```text
POST /agent/chat { conversation_id, agent_id?, message }
   ↓ get_current_user（JWT）
AgentService.chat()
   ├─ 会话归属校验（不存在 404 / 非本人 403）
   ├─ 读取 Agent 配置（provider / model / temperature / system_prompt）
   ├─ 绑定知识库 → RAG 检索 → 组装 context
   └─ 工具子集 → ToolRegistry
   ↓
AgentRuntime.run() → LLMProvider.generate() → LLM
   ↓
持久化 user + assistant 消息（attach 到会话）
   ↓
200 { conversation_id, reply }
```

### 设计原则

- **Router 保持薄层**：不访问数据库 / Repository / Session，不写业务规则，只做协议转换
- **Service 管理业务逻辑**：权限、编排、跨资源校验集中在 Service 层
- **Runtime 不依赖数据库**：Agent 执行引擎与持久化、HTTP 框架完全解耦，可独立测试
- **Provider 可替换**：新增 LLM 厂商只需实现 `LLMProvider` 接口并在工厂注册，Runtime / Service 无需改动
- **唯一事务边界**：只有 `app/database/session.py` 的 `get_db` 负责 `commit()` / `rollback()`，且以 `scope="function"` 声明，保证「响应返回时事务已提交」，避免写后读不可见
- **统一错误响应**：所有异常由全局处理器收敛为 `{"error": {"code": "...", "message": "..."}}`，不泄露密钥 / SQL / traceback

---

## Project Structure

```text
Enterprise-AI-Agent-Platform/
├── backend/
│   ├── alembic/
│   │   └── versions/                  # 数据库迁移（001 ~ 007）
│   ├── app/
│   │   ├── api/                       # HTTP 路由层（薄层）
│   │   │   ├── admin.py               #   /admin/*            平台概览 + 用户管理
│   │   │   ├── agent.py               #   /agent/chat         Agent 对话
│   │   │   ├── agents.py              #   /agents/*           Agent 配置管理
│   │   │   ├── auth.py                #   /auth/login         登录签发 JWT
│   │   │   ├── conversations.py       #   /conversations/*    会话与消息历史
│   │   │   ├── knowledge_bases.py     #   /knowledge-bases/*  知识库 / 文档 / 上传 / 索引
│   │   │   └── users.py               #   /users/*            注册与用户自助
│   │   ├── agents/                    # Agent 运行时（框架无关）
│   │   │   ├── runtime.py             #   执行循环 + Tool Calling + 上下文组装
│   │   │   ├── execution.py           #   一次执行的状态模型（AgentExecution）
│   │   │   ├── observers/             #   执行可观测性（Observer 抽象 + 日志实现）
│   │   │   ├── providers/             #   LLM Provider 抽象与工厂（qwen / ollama）
│   │   │   └── tools/                 #   Tool 抽象 / Registry / 参数校验 / 内置工具
│   │   ├── core/                      # 配置、依赖注入、JWT 与 bcrypt、异常与处理器、角色
│   │   ├── database/                  # 引擎、会话工厂、唯一事务边界
│   │   ├── middleware/                # CORS
│   │   ├── models/                    # ORM：user / conversation / message / agent / agent_tool / knowledge_base / document
│   │   ├── rag/                       # loader / splitter / embeddings / vectorstore / retriever
│   │   │   └── build_index.py         #   离线自检 CLI（不在运行时链路中）
│   │   ├── repositories/              # 数据访问层（只 flush，不 commit）
│   │   ├── schemas/                   # Pydantic 请求 / 响应模型
│   │   └── services/                  # 业务逻辑：auth / user / admin / agent / knowledge_base
│   ├── scripts/                       # 验收与维护脚本（见下方 Verification Scripts）
│   ├── alembic.ini
│   ├── pyproject.toml
│   ├── uv.lock
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── api/                       # 接口封装：auth / user / conversation / agent / knowledgeBase / admin
│   │   ├── assets/                    # 全局样式
│   │   ├── components/                # Sidebar / ConversationList / MessageList / MessageInput
│   │   ├── layouts/                   # MainLayout（顶栏 + 模块导航 + 内容区）
│   │   ├── router/                    # 路由表 + 登录 / 管理员守卫
│   │   ├── stores/                    # Pinia：auth / user / chat / conversation / agent / knowledgeBase / dashboard / admin
│   │   ├── utils/                     # request（唯一 Axios 实例）/ authToken / error
│   │   ├── views/                     # LoginView / ChatView / AgentManagementView / KnowledgeBaseView / AdminView
│   │   ├── App.vue
│   │   └── main.ts
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts                 # 开发端口固定 5174（strictPort）
│   └── .env.example
├── .gitignore
└── README.md
```

前端页面与路由：

| 路由 | 页面 | 权限 |
| --- | --- | --- |
| `/login` | LoginView（登录 + 注册切换） | 公开 |
| `/app/chat[/:conversationId]` | ChatView（AI 工作台：会话列表 + 消息 + 输入） | 登录用户 |
| `/app/agents` | AgentManagementView（Agent 配置管理） | 管理员 |
| `/app/knowledge` | KnowledgeBaseView（知识库 / 文档 / 上传 / 索引） | 管理员 |
| `/app/admin` | AdminView（平台概览 + 用户管理） | 管理员 |

---

## Installation

### Prerequisites

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | >= 3.14 | 见 `backend/pyproject.toml` |
| uv | 最新版 | 依赖与虚拟环境管理（项目按 uv 组织：`pyproject.toml` + `uv.lock`） |
| PostgreSQL | 13+ | 需自行准备实例与数据库；仓库不含 docker-compose |
| Node.js | ^22.18.0 或 >= 24.12.0 | 见 `frontend/package.json` 的 `engines` |
| npm | 随 Node 安装 | 前端依赖与脚本 |
| Ollama（可选） | — | 使用本地 LLM / 本地 Embedding 时需要 |
| LLM API Key（可选） | — | 使用 Qwen 时需要（DashScope 兼容模式密钥） |

> ChromaDB 作为 Python 依赖内嵌运行，**无需单独部署向量数据库**；向量数据保存在 `backend/.chroma`（已被 `.gitignore` 忽略）。

### 1. Backend

```bash
cd backend

# 安装依赖（读取 pyproject.toml + uv.lock）
uv sync

# 准备环境变量
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
# 至少填写 DATABASE_URL、SECRET_KEY；使用 Qwen 时再填写 LLM_API_KEY

# 创建 / 升级数据库结构
uv run alembic upgrade head

# 启动服务（开发模式，端口 8001）
uv run uvicorn app.main:app --reload --port 8001
```

启动后可访问：

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8001/` | 根路径：`{"message": "Enterprise AI Agent Platform API"}` |
| `http://localhost:8001/docs` | Swagger UI（自动生成的 OpenAPI 文档） |

### 2. Frontend

```bash
cd frontend

npm install

cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
# VITE_API_BASE_URL=http://localhost:8001

npm run dev                   # 访问 http://localhost:5174
```

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 开发服务器，端口固定 `5174`（`strictPort: true`，被占用时直接启动失败，不会自动切换端口） |
| `npm run build` | 类型检查 + 生产构建 |
| `npm run type-check` | 仅执行 `vue-tsc` 类型检查 |

### 3. 首次使用

1. 打开 `http://localhost:5174/login`，切换到「注册」创建账号（默认角色 `user`）。
2. 将账号提升为管理员（见下方「管理员账号」），重新登录后即可进入 Agent 管理 / 知识库 / 系统管理。
3. **知识库**：创建知识库 → 添加正文或上传文档（txt / md / pdf / docx）→ 点击索引。
4. **Agent 管理**：创建 Agent，配置 provider / model / temperature / 系统提示词 / 绑定知识库 / 绑定工具。
5. **AI 工作台**：选择 Agent，开始多轮对话。

### 4. 关键配置项（`backend/.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 空 | PostgreSQL 异步连接串，格式 `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_KEY` | `change-this-secret-key` | JWT 签名密钥，**生产环境必须替换** |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `30` | JWT 算法与有效期 |
| `ENVIRONMENT` | `development` | `development` 时仅允许 `http://localhost:5174` 跨域访问 |
| `LLM_PROVIDER` | `qwen` | LLM Provider：`qwen` 或 `ollama` |
| `LLM_BASE_URL` | 空 | 留空使用 Provider 内置默认端点 |
| `LLM_API_KEY` | 空 | Qwen / DashScope 兼容模式密钥 |
| `LLM_MODEL` | `qwen` | 默认模型名 |
| `LLM_TEMPERATURE` | `0.7` | 默认生成温度（0.0 - 2.0） |
| `AGENT_TOOL_CALLING_ENABLED` | `true` | 是否启用 Tool Calling；关闭后 Agent 退化为纯文本对话 |
| `VECTOR_STORE_DIR` | `.chroma` | ChromaDB 持久化目录（相对 `backend/`） |
| `VECTOR_COLLECTION_NAME` | `knowledge_chunks` | collection 名前缀，实际使用 `{值}__kb_{知识库 ID}` |
| `EMBEDDING_PROVIDER` | `hash` | `hash`（本地散列，仅链路自检）/ `ollama` / `dashscope`；**真实语义检索请配置 `dashscope` 或 `ollama`** |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` | 空 | 与所选 Embedding Provider 配套 |
| `RAG_TOP_K` | `4` | 检索返回的知识片段数 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `500` / `50` | 文本切分长度与重叠 |
| `MAX_UPLOAD_FILE_SIZE_MB` | `10` | 单个上传文件大小上限（超出返回 413） |

前端配置：`frontend/.env` 的 `VITE_API_BASE_URL`（默认 `http://localhost:8001`；未配置时前端也会回退到该地址）。

---

## API Overview

所有接口均可在 `http://localhost:8001/docs` 中直接调试；除公开接口外，请在 `Authorization: Bearer <token>` 中携带登录令牌。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/users/` | 公开 | 注册用户（用户名 / 邮箱全局唯一） |
| `POST` | `/auth/login` | 公开 | 登录，返回 `{access_token, token_type}` |
| `GET` | `/users/me` | 登录 | 当前登录用户信息 |
| `GET / PATCH / DELETE` | `/users/{id}` | 登录（仅本人） | 查看 / 更新 / 删除自己的账号；管理员请走 `/admin/users/*` |
| `POST` | `/conversations` | 登录 | 创建会话 |
| `GET` | `/conversations?page=&page_size=` | 登录（仅本人） | 分页获取自己的会话列表 |
| `GET / PATCH / DELETE` | `/conversations/{id}` | 登录（仅本人） | 会话详情 / 重命名 / 删除 |
| `GET` | `/conversations/{id}/messages` | 登录（仅本人） | 会话历史消息（只读） |
| `POST` | `/agent/chat` | 登录 | 发送消息：`{conversation_id, agent_id?, message}` → `{conversation_id, reply}` |
| `GET` | `/agents/available` | 登录 | 工作台可选的 Agent 列表（仅 id / name / description） |
| `GET` | `/agents/tools` | 管理员 | 后端已注册工具列表（Agent 工具绑定选项来源） |
| `GET / POST` | `/agents` | 管理员 | Agent 列表 / 创建 |
| `GET / PATCH / DELETE` | `/agents/{id}` | 管理员 | Agent 详情 / 更新 / 删除 |
| `GET / POST` | `/knowledge-bases` | 管理员 | 知识库列表 / 创建（`POST` 支持 JSON、urlencoded、multipart 三种形态，multipart 可同时导入首份文档） |
| `GET / PATCH / DELETE` | `/knowledge-bases/{id}` | 管理员 | 知识库详情 / 更新 / 删除（级联文档并清理向量 collection） |
| `GET / POST` | `/knowledge-bases/{id}/documents` | 管理员 | 文档列表（不含正文）/ 创建文档 |
| `GET / PATCH / DELETE` | `/knowledge-bases/{id}/documents/{doc_id}` | 管理员 | 文档详情（含正文）/ 更新（重置索引状态）/ 删除（重建索引） |
| `POST` | `/knowledge-bases/{id}/documents/upload` | 管理员 | `multipart/form-data` 上传文件并解析为文档正文 |
| `POST` | `/knowledge-bases/{id}/documents/{doc_id}/index` | 管理员 | 建立 / 重建该知识库索引，返回真实 `status` |
| `GET` | `/admin/dashboard` | 管理员 | 平台资源实时统计 |
| `GET` | `/admin/users` | 管理员 | 分页用户列表 |
| `GET` | `/admin/users/{id}` | 管理员 | 查看指定用户 |
| `PATCH` | `/admin/users/{id}/role` | 管理员 | 修改用户角色（`user` ⇄ `admin`） |
| `DELETE` | `/admin/users/{id}` | 管理员 | 删除用户（204） |

错误响应统一为：

```json
{
  "error": {
    "code": "AUTHENTICATION_REQUIRED",
    "message": "请先登录"
  }
}
```

---

## 管理员账号

只有在**存在 admin 账号**时才能进入 `/app/admin`、`/app/agents`、`/app/knowledge`。若库中尚无管理员，可先注册账号（默认 `user`），再执行：

```sql
UPDATE users SET role = 'admin' WHERE username = '你的用户名';
```

重新登录后前端即可看到管理入口。

---

## Development Notes

### 端口约定

本项目的端口与另一个项目 **Travel-H5** 严格区分，两个项目可以同时在本机运行：

| 项目 | 前端 | 后端 |
| --- | --- | --- |
| **Enterprise-AI-Agent-Platform（本项目）** | `http://localhost:5174` | `http://localhost:8001` |
| Travel-H5（本项目不修改） | 5173 | 8000 |

- 前端 dev server 端口已在 `frontend/vite.config.ts` 中固定为 `5174` 且 `strictPort: true`：端口被占用时启动直接失败，**不会**自动切换到 5175；
- 开发环境 CORS 仅允许 `http://localhost:5174`（`backend/app/middleware/cors.py`），请使用该地址访问前端。

### 权限双层

- **体验层**：前端路由守卫（`meta.requiresAdmin`）+ 菜单隐藏，非管理员访问管理页面会被重定向回 AI 工作台并提示无权限；
- **真实边界**：后端 `require_admin` 独立强制，普通用户直接调用管理接口依然返回 403 —— 前端隐藏按钮不构成任何安全依赖。

### 事务与错误响应

- **唯一事务边界**：只有 `app/database/session.py` 的 `get_db` 负责 `commit()` / `rollback()`，并以 `scope="function"` 声明，保证「接口返回成功时事务已提交」，写后立即读可见；
- **统一错误结构**：业务异常 / HTTPException / 参数校验 / 未预期异常全部收敛为 `{"error": {"code", "message"}}`，不泄露密钥、SQL 与 traceback；
- **不伪造结果**：统计来自数据库实时查询，索引接口返回真实成功 / 失败状态，脚本在缺少 LLM 凭据时输出 SKIP 而不是伪造通过。

### 前端约定

- 所有请求都通过 `frontend/src/utils/request.ts` 的同一个 Axios 实例发出：自动附加 Bearer Token、统一错误归一化、401 自动清理登录态并跳回登录页；
- 页面状态集中在 Pinia store（`src/stores/*`），页面与组件不直接调用 Axios。

---

## Verification Scripts

`backend/scripts/` 下的脚本用于端到端验收与回归验证：全部**进程内驱动真实应用**（真实 JWT 认证、真实数据库、真实向量库），仅在需要固定 LLM 输出时用 spy 替换 Provider（不伪造业务结果，也不会消耗额度）；脚本结束时清理自己创建的测试数据，且不读取 / 不打印任何密钥。

```bash
cd backend
export PYTHONPATH=$(pwd)        # Windows PowerShell: $env:PYTHONPATH = (Get-Location).Path

uv run python scripts/permission_audit_test.py
uv run python scripts/final_acceptance_test.py
```

| 脚本 | 验证内容 |
| --- | --- |
| `permission_audit_test.py` | guest / user / admin 全量权限矩阵、会话归属隔离、越权不泄露内容 |
| `final_acceptance_test.py` | 端到端完整链路：Admin → 知识库 → 文档 → 索引 → Agent → 工具 → User → 对话 → RAG → LLM（无 LLM 凭据时明确 SKIP） |
| `admin_user_management_acceptance_test.py` | `/admin/users*` 契约与权限：后端分页、角色升降级、删除用户、自改 / 自删 403、非法角色 422、前端接线静态检查 |
| `transaction_boundary_acceptance_test.py` | 写接口返回成功时事务已提交（提交早于响应，写后立即读可见） |
| `exception_handling_acceptance_test.py` | 统一错误响应结构，401 / 403 / 404 / 422 / 502 / 500 语义，敏感信息不外泄 |
| `agent_runtime_acceptance_test.py` | 执行循环与边界：普通回答、单 / 多工具调用、未知工具、参数错误、最大轮数、消息上下文不被污染 |
| `execution_observability_acceptance_test.py` | 执行可观测性：Observer 钩子时序、工具调用与耗时、Observer 异常不影响主流程 |
| `provider_acceptance_test.py` | Provider 工厂（qwen / ollama）、未知 Provider 明确报错、Runtime 与具体 Provider 解耦、温度透传 |
| `tool_acceptance_test.py` | Calculator 正确性与安全性、Registry 行为、Tool Schema、Tool Calling 全流程 |
| `tool_validation_acceptance_test.py` | 多工具注册、Schema 驱动参数校验、两个内置工具的完整调用链 |
| `tool_regression_test.py` | 普通 / 多轮对话、RAG 注入路径、会话 CRUD、认证的 HTTP 端到端回归 |
| `rag_acceptance_test.py` | 对话接口、历史消息 + 检索上下文同时生效、无凭据不伪造 RAG 成功、既有功能无回归 |
| `kb_file_upload_acceptance_test.py` | 文件上传：txt / md / pdf / docx 解析、索引入库、删除级联、权限与安全边界（扩展名 / 空文件 / 超大 / 二进制 / 目录穿越） |
| `kb_create_with_upload_acceptance_test.py` | `POST /knowledge-bases` 三种请求形态（JSON / urlencoded / multipart）、「建库 + 首份文档」同事务语义、失败整体回滚 |
| `chroma_collection_lifecycle_acceptance_test.py` | Chroma collection 生命周期：建库不建 collection、索引后与物理目录一一对应、删除后真正消失、多库隔离、重建索引幂等 |
| `default_rag_removal_acceptance_test.py` | 平台不预置默认 RAG 数据、默认对话不执行 RAG、正式 RAG 完全由知识库管理驱动 |
| `cleanup_orphan_chroma_storage.py` | **维护工具**（非测试）：在服务停止后清理向量目录中的孤儿数据与运行期延迟残留 |

> 部分脚本会访问运行中的后端 `http://127.0.0.1:8001`，请先启动后端并完成 `alembic upgrade head`。

---

## License

本仓库当前未包含开源许可证文件（无 `LICENSE`）。如需商用或二次分发，请先与作者确认授权。
