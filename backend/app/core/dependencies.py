"""FastAPI 统一依赖注入入口。

本模块的职责
--------------

1. **统一管理入口**：`dependencies.py` 是 FastAPI 项目中所有依赖注入定义的
   集中汇聚点。无论依赖来自 `database/session.py`、`services/`、还是 `repositories/`，
   最终都从这里统一导出，供 Router 层使用。

2. **`get_db()` 的来源**：
   ``get_db`` 在 ``backend/app/database/session.py`` 中定义和实现——
   那里负责创建异步数据库引擎（``create_async_engine``）、会话工厂
   （``async_sessionmaker``）以及生成器函数 ``get_db``。
   本模块通过 ``from app.database.session import get_db`` 将其导入并重新导出。

3. **为什么不在 dependencies.py 中重新创建数据库 Session**：
   - **单一数据源（Single Source of Truth）**：engine 和 session 的配置
     （连接池大小、echo 模式、expire_on_commit 等）只应在 ``session.py``
     中定义一次。如果在多处重复创建，会导致配置不一致和维护困难。
   - **避免循环导入**：``session.py`` 依赖 ``config.py`` 获取数据库 URL；
     如果 ``dependencies.py`` 也创建 engine，调用关系会变得更复杂。
   - **职责分离**：``session.py`` 管"怎么建连接"，``dependencies.py``
     管"怎么注入给 FastAPI"，各司其职。

4. **统一依赖注入风格**：

   所有 Service 依赖 `get_user_service / get_auth_service / get_admin_service`
   都采用 FastAPI 原生 ``Depends`` 链，并统一使用 :data:`DbSession`
   （``scope="function"``，保证 commit 在响应发出之前完成）::

       async def get_service(
           db: DbSession,
       ) -> Service:
           repo = Repository(db)
           return Service(repo)

   不再使用手动 ``anext()`` 驱动 ``AsyncGenerator``。
   这样：
   - FastAPI 自动解析 ``get_db`` 生成器并管理其生命周期；
   - commit/rollback 由 ``get_db`` 统一负责（唯一事务边界）；
   - 同一个请求内，同一依赖链上的所有依赖共享同一个 Session；
   - 事务在响应字节发出之前提交，写接口返回成功后数据立即可见。

5. **为什么集中管理依赖有利于测试和维护**：
   - **测试时替换依赖**：可以使用 ``app.dependency_overrides`` 将 ``get_db``
     替换为测试数据库的会话工厂，无需修改任何业务代码。
   - **依赖关系一目了然**：新人查看 ``dependencies.py`` 就能了解整个项目
     使用了哪些基础设施依赖。
   - **减少导入路径碎片化**：所有 Router 和 Service 只需记住
     ``from app.core.dependencies import ...`` 一个入口，不需要关心
     底层模块的具体路径。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.observers import (
    AgentExecutionObserver,
    build_default_observers,
)
from app.agents.providers import get_llm_provider
from app.agents.runtime import AgentRuntime
from app.agents.tools import ToolRegistry, build_default_registry
from app.core.config import settings
from app.core.exceptions import PermissionDeniedException
from app.core.roles import ADMIN_ROLE
from app.database.session import get_db
from app.models.user import User
from app.rag.embeddings import get_embedding_provider
from app.rag.exceptions import RAGException
from app.rag.retriever import Retriever
from app.rag.vectorstore import VectorStore
from app.repositories.agent import AgentRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.document import DocumentRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.services.admin import AdminService
from app.services.agent import AgentService
from app.services.auth import AuthService
from app.services.knowledge_base import (
    KnowledgeBaseService,
    knowledge_base_collection_name,
)
from app.services.user import UserService


# ------------------------------------------------------------------
# 统一数据库会话依赖（事务作用域 = function）
# ------------------------------------------------------------------
# 为什么必须显式声明 ``scope="function"``
# -------------------------------------
# FastAPI 对 ``yield`` 依赖的关闭时机取决于依赖作用域
# （见 ``fastapi.routing.request_response`` 与
# ``fastapi.dependencies.utils.solve_dependencies``）::
#
#     scope="request"（默认）
#         response = await f(request)     # 依赖注入 + handler + 响应序列化
#         await response(...)             # ← 响应字节先发给客户端
#         关闭 request 作用域依赖栈        # ← get_db 的 commit() 在这里才执行
#
#     scope="function"
#         response = await f(request)     # 依赖注入 + handler + 响应序列化
#         关闭 function 作用域依赖栈       # ← get_db 的 commit() 在这里执行
#         await response(...)             # ← 响应字节最后才发给客户端
#
# 使用默认作用域会出现「写接口已返回 201，但紧接着的请求读不到数据」的
# 可见性竞态（提交发生在响应发出之后）。声明为 ``function`` 后，
# 事务在响应字节发出之前提交，满足：
#
#     写入数据库 → commit → 响应返回给客户端
#
# 注意：作用域参与 FastAPI 的依赖缓存键（``_get_cache_key``），
# 同一请求内必须全部使用本别名；混用 ``Depends(get_db)`` 会被视为
# 不同依赖并创建两个 Session。
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


async def get_user_service(
    db: DbSession,
) -> UserService:
    """依赖注入：构建并返回 UserService 实例。

    依赖注入链路
    ------------

        Router
          ↓  Depends(get_user_service)
        get_user_service()
          ↓  DbSession                 ← FastAPI 自动注入 AsyncSession
                                       （scope="function"：响应发出前提交）
          ↓  UserRepository(db)
          ↓  UserService(repository)
          ↓  return 给 Router

    Router 只需声明"我需要 UserService"，无需知道 Repository 和
    Session 的存在。Session 与事务边界由 ``get_db`` 统一管理。
    """
    repo = UserRepository(db)
    return UserService(repo)


async def get_auth_service(
    db: DbSession,
) -> AuthService:
    """依赖注入：构建并返回 AuthService 实例。

    与 ``get_user_service`` 使用完全相同的 ``DbSession`` 依赖
    （``scope="function"``：提交发生在响应发出之前），
    Router 只需声明"我需要 AuthService"，无需知道 Repository 和 Session。
    """
    repo = UserRepository(db)
    return AuthService(repo)


async def get_admin_service(
    db: DbSession,
) -> AdminService:
    """依赖注入：构建并返回 AdminService 实例。

    与 ``get_user_service`` / ``get_auth_service`` 统一使用 ``DbSession``
    依赖，Router 无需知道 Repository/Session 的存在。

    注入的 Repository（Stage 18）：用户管理 + Dashboard 概览统计所需的
    6 张表（users / agents / knowledge_bases / documents / conversations /
    messages）。所有查询共用同一个请求级 Session 与事务边界。
    """
    return AdminService(
        user_repository=UserRepository(db),
        agent_repository=AgentRepository(db),
        conversation_repository=ConversationRepository(db),
        message_repository=MessageRepository(db),
        knowledge_base_repository=KnowledgeBaseRepository(db),
        document_repository=DocumentRepository(db),
    )


async def get_knowledge_base_service(
    db: DbSession,
) -> KnowledgeBaseService:
    """依赖注入：构建并返回 KnowledgeBaseService 实例（Stage 16）。

    构建链路::

        Router
          ↓  Depends(get_knowledge_base_service)
        get_knowledge_base_service()
          ↓  DbSession（scope="function"：响应发出前提交）
          ↓  KnowledgeBaseRepository(db)
          ↓  DocumentRepository(db)
          ↓  KnowledgeBaseService(...)
          ↓  return 给 Router

    说明：本 Service 只操作数据库元数据与 RAG 模块（ChromaDB），
    **不依赖 AgentRuntime / LLMProvider / ToolRegistry**，
    也不触碰默认向量 collection（Agent 对话检索行为完全不变）。
    """
    return KnowledgeBaseService(
        knowledge_base_repository=KnowledgeBaseRepository(db),
        document_repository=DocumentRepository(db),
    )


def get_tool_registry() -> ToolRegistry | None:
    """依赖注入：构建 Agent 可用的 Tool Registry。

    - ``AGENT_TOOL_CALLING_ENABLED=true``  → 返回内置工具注册表（Calculator、DateTime）；
    - ``AGENT_TOOL_CALLING_ENABLED=false`` → 返回 ``None``，
      AgentRuntime 自动退化为纯文本对话。

    采用「代码级内置工具」方式：不引入 Tool 数据库表、动态插件加载、
    远程 Registry 或复杂权限系统。
    """
    if not settings.AGENT_TOOL_CALLING_ENABLED:
        return None
    return build_default_registry()


def build_agent_runtime(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    tool_names: list[str] | None = None,
) -> AgentRuntime:
    """按 Agent 配置构建 AgentRuntime（Stage 17）。

    - ``provider`` / ``model`` / ``temperature``：来自 Agent 配置；为 ``None``
      时沿用全局 ``settings``（保持 Stage 17 之前的行为完全不变）；
    - ``tool_names``：该 Agent 绑定的工具名列表。为 ``None`` 时使用全部内置工具
      （既有行为）；为 ``[]`` 时不向 LLM 暴露任何工具（Runtime 自动退化为
      纯文本对话，见 ``AgentRuntime._available_tool_schemas``）。

    工具子集通过 ``ToolRegistry(tools=[...])`` 构造 —— **不修改 ToolRegistry /
    Tool 实现**，也不存在任何动态代码加载（无 eval / exec / 动态 import）。
    """
    llm_provider = get_llm_provider(
        provider=provider, model=model, temperature=temperature
    )

    registry = get_tool_registry()
    if registry is not None and tool_names is not None:
        allowed = set(tool_names)
        registry = ToolRegistry(
            tools=[
                tool for tool in registry.list_tools() if tool.name in allowed
            ]
        )

    return AgentRuntime(
        llm_provider,
        tool_registry=registry,
        observers=get_execution_observers(),
    )


def build_knowledge_base_retriever(
    knowledge_base_id: int,
) -> Retriever | None:
    """为指定知识库构建 Retriever（Stage 17）。

    复用 Stage 16 的 collection 命名约定
    （``app.services.knowledge_base.knowledge_base_collection_name``），
    确保检索的是**该知识库自己的向量集合**。

    任何 RAG 初始化失败（embedding 配置缺失 / ChromaDB 异常）都返回 ``None``，
    使该次对话退化为「无知识库」正常对话，而不是让 Chat 失败。
    """
    try:
        return Retriever(
            embedding_provider=get_embedding_provider(),
            vector_store=VectorStore(
                collection_name=knowledge_base_collection_name(
                    knowledge_base_id
                )
            ),
        )
    except (RAGException, Exception):  # noqa: BLE001
        return None


def get_execution_observers() -> list[AgentExecutionObserver]:
    """依赖注入：构建 Agent 执行的观察者列表（Stage 9）。

    当前返回 ``[LoggingExecutionObserver()]``——用标准库 logging 记录
    一次执行的开始 / 每次 Tool 调用 / 结束（含耗时）。

    设计说明
    --------
    - **每次请求新建实例**：不使用全局单例、不持有全局可变状态；
    - **抽象注入**：AgentRuntime 只声明需要
      ``AgentExecutionObserver`` 抽象，不感知具体实现；
    - **无副作用开关**：返回空列表即等同于关闭可观测性，
      Agent 执行行为完全不变；
    - **只读观察**：观察者不接触 Database / Repository / RAG，
      也不会写任何业务数据。

    Returns:
        list[AgentExecutionObserver]: 默认启用的观察者（当前为 1 个）。
    """
    return build_default_observers()


async def get_agent_service(
    db: DbSession,
) -> AgentService:
    """依赖注入：构建并返回 AgentService 实例。

    构建链路（Stage 20 起）::

        build_agent_runtime()          # 平台默认：Provider / Model / Temperature + 内置工具
            ↓
        AgentService(
            db,
            runtime,                   # 平台默认执行引擎（agent_id 未指定时使用）
            retriever=None,            # ★ 平台默认不绑定任何知识库（不再有默认 RAG）
            runtime_factory=...,
            retriever_factory=...,     # ★ 仅当 Agent 绑定 knowledge_base_id 时才构建检索器
        )

    RAG 边界（Stage 20 变更，重要）
    ------------------------------
    - **平台默认对话（``agent_id`` 未指定）不再执行 RAG**：不再创建指向默认
      collection（``settings.VECTOR_COLLECTION_NAME``）的 ``Retriever``，
      因此也不存在「预置知识数据」这一概念；
    - 正式 RAG 知识全部由管理员经 **Knowledge Base → Document → Index** 管理；
    - 只有显式指定 ``agent_id`` 且该 Agent 绑定了 ``knowledge_base_id`` 时，
      才通过 ``retriever_factory``（:func:`build_knowledge_base_retriever`）
      构建**该知识库自己的 collection** 的 ``Retriever``；
    - 平台默认 Provider / Model / Temperature / 工具能力**完全保留**
      （见 :func:`build_agent_runtime`）。
    """
    return AgentService(
        db,
        build_agent_runtime(),
        retriever=None,
        runtime_factory=build_agent_runtime,
        retriever_factory=build_knowledge_base_retriever,
    )


# ------------------------------------------------------------------
# JWT 认证安全方案
# ------------------------------------------------------------------
# HTTPBearer 自动从 Authorization header 提取 Bearer token。
# auto_error=True（默认）时，缺少或格式错误的 header 会自动返回 403。
# 我们手动设置为 False，统一返回 401 + "Could not validate credentials"。
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: DbSession,
) -> User:
    """从 JWT 令牌中解析并返回当前登录用户。

    这是受保护路由的核心依赖——任何需要登录才能访问的接口，
    只需在路径函数参数中添加 ``current_user: User = Depends(get_current_user)``。

    完整流程
    --------

    1. 从 ``Authorization: Bearer <token>`` 头提取 JWT 字符串。
        由 ``HTTPBearer`` 完成，若缺失或格式错误 → 401。
    2. 使用 ``jose.jwt.decode()`` 验证签名和过期时间。
        密钥和算法来自 ``settings.SECRET_KEY`` / ``settings.ALGORITHM``。
    3. 从 payload 中提取 ``sub`` 字段，转换为 ``int`` 类型的 ``user_id``。
    4. 通过 ``UserRepository`` 查询数据库中的用户。
    5. 用户存在 → 返回 ``User`` ORM 对象。
    6. 任何步骤失败 → 统一返回 401 ``"Could not validate credentials"``。

    安全设计
    --------

    - **错误消息统一**：JWT 过期、签名错误、sub 缺失、用户不存在……
      所有失败场景返回完全相同的 401 消息，不泄露任何内部细节。
    - **WWW-Authenticate 头**：每个 401 响应均包含 ``WWW-Authenticate: Bearer``，
      符合 RFC 7235 规范。
    - **不直接操作数据库**：通过 ``UserRepository`` 间接访问，
      Router 和本依赖均不调用 ``session.execute()`` 或 ``select()``。

    Returns
    -------
    User
        当前已认证的用户 ORM 对象。

    Raises
    ------
    HTTPException (401)
        当 token 缺失 / 无效 / 过期 / payload 异常 / 用户不存在时抛出。
    """
    # 1. 检查 Authorization header 是否存在
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # 2. 解码并验证 JWT（签名 + 过期时间）
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except JWTError:
        # JWTError 涵盖：签名错误、过期、格式错误等所有 jose 异常
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. 提取 sub（user_id）
    user_id_str: str | None = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. sub 必须是有效的整数
    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 5. 通过 Repository 查询用户（不直接使用 AsyncSession / select / execute）
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)

    # 6. 用户必须存在
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """管理员权限依赖。

    依赖于 ``get_current_user`` 完成身份认证后，
    进一步检查当前用户是否具有管理员角色。

    使用方式（在 Router 中）::

        @router.delete("/users/{user_id}")
        async def admin_only_route(
            admin: Annotated[User, Depends(require_admin)],
        ):
            # admin 已经通过 JWT 认证 + 角色检查
            ...

    完整依赖链::

        Depends(require_admin)
                ↓
        require_admin()
                ↓
        Depends(get_current_user)  ← 认证
                ↓
        get_current_user()
                ↓  JWT decode → user_id → UserRepository → User
                ↓  认证失败 → 401
                ↓
        require_admin() 继续：
                ↓  检查 current_user.role == ADMIN_ROLE
                ↓  不是 admin → PermissionDeniedException → 403
                ↓  是 admin → return current_user

    Returns:
        User: 已通过 JWT 认证且角色为 admin 的用户 ORM 对象。

    Raises:
        PermissionDeniedException: 当用户角色不是 admin 时抛出。
            Router 层会将其映射为 HTTP 403。
    """
    if current_user.role != ADMIN_ROLE:
        raise PermissionDeniedException(
            "Admin privileges required",
        )
    return current_user


__all__ = [
    "DbSession",
    "bearer_scheme",
    "get_admin_service",
    "get_agent_service",
    "get_auth_service",
    "get_current_user",
    "get_db",
    "get_execution_observers",
    "get_knowledge_base_service",
    "get_tool_registry",
    "get_user_service",
    "require_admin",
]
