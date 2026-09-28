"""Agent Service 层。

本 Service 承载同一 Agent 领域的两类业务：

1. **Agent 对话执行**：读取 Agent 配置（provider / model / temperature /
   system_prompt / 知识库 / 工具绑定）→ 编排 RAG 与 Tool Calling →
   ``AgentRuntime`` → ``LLMProvider``（Stage 17）；
2. **Agent 配置管理**（Stage 15）：``/agents`` 的 CRUD。

约束：不依赖任何 Web 框架类型（不出现请求 / 响应 / 依赖注入 / 框架异常类）；
不执行 commit / rollback（由 ``get_db`` 统一负责）。

配置如何生效（Stage 17；Stage 20 起默认不使用 RAG）
--------------------------------------------------
``POST /agent/chat`` 可携带 ``agent_id``：

    未指定 agent_id → 平台默认（全局 settings 的 Provider/Model/Temperature +
                      全部内置工具），**不执行 RAG**（不绑定任何知识库）
    指定 agent_id   → 使用该 Agent 的 provider / model / temperature /
                      system_prompt / 知识库 collection / 工具子集

正式 RAG 知识由管理员经 **Knowledge Base → Document → Index** 管理
（Stage 20：项目不再预置任何默认知识数据）。

构建工作全部在**本 Service** 完成（通过 DI 注入的两个工厂），
Runtime / Provider / Retriever 本身**不认识 Agent 模型，也不访问数据库**。

RAG 集成
--------
- Agent 绑定知识库时：用该知识库的 collection 检索，context 与 system_prompt
  合并为**唯一一条** system 消息（不产生两个 system）；
- Agent 未绑定知识库时：不执行检索（不影响对话）；
- 检索异常时静默降级，不阻断主链路（RAG 是增强能力）。
"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.providers import SUPPORTED_PROVIDERS
from app.agents.runtime import AgentRuntime
from app.agents.tools import Tool, build_default_registry
from app.core.exceptions import (
    AppException,
    PermissionDeniedException,
    ResourceNotFoundException,
)
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import MESSAGE_ROLE_ASSISTANT, MESSAGE_ROLE_USER, Message
from app.models.user import User
from app.rag.retriever import Retriever
from app.repositories.agent import AgentRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.message import MessageRepository
from app.schemas.agent import AgentCreate, AgentUpdate

#: RAG 参考资料块（不含角色设定，便于与 Agent 自己的 system_prompt 合并）
RAG_CONTEXT_INSTRUCTION = (
    "以下是知识库提供的参考资料：\n"
    "[Context]\n"
    "{context}\n\n"
    "请基于参考资料回答用户问题。如果参考资料中没有足够信息，"
    "不要编造，请明确告诉用户当前知识库中没有足够信息。"
)

#: 未配置 Agent 提示词时的默认 RAG System Prompt（文本与 Stage 16 之前完全一致）
RAG_SYSTEM_PROMPT_TEMPLATE = (
    "你是企业级 AI Agent 助手。请用简洁、专业、中文回答用户问题。\n\n"
    + RAG_CONTEXT_INSTRUCTION
)


class AgentService:
    """Agent 业务逻辑服务。

    Args:
        db: 请求级 AsyncSession（由 get_db 注入）。
        runtime: 默认执行引擎（未指定 Agent 时使用；由 get_agent_service 注入）。
        retriever: 可选的默认 RAG 检索器。**平台 DI 传 ``None``**（Stage 20：
            默认对话不再执行 RAG）；保留该参数是为了让测试或其它调用方
            在需要时显式注入检索器。
        runtime_factory: 按 Agent 配置构建 Runtime 的工厂（Stage 17）。
            未注入时退化为「始终使用默认 runtime」。
        retriever_factory: 按知识库 ID 构建 Retriever 的工厂（Stage 17）。
            未注入时退化为「使用默认 retriever（通常为 None）」。
    """

    def __init__(
        self,
        db: AsyncSession,
        runtime: AgentRuntime,
        retriever: Retriever | None = None,
        runtime_factory: (
            Callable[..., AgentRuntime] | None
        ) = None,
        retriever_factory: (
            Callable[[int], Retriever | None] | None
        ) = None,
    ) -> None:
        self.db = db
        self.runtime = runtime
        self.retriever = retriever
        self.runtime_factory = runtime_factory
        self.retriever_factory = retriever_factory
        self.conversation_repo = ConversationRepository(db)
        self.message_repo = MessageRepository(db)
        self.agent_repo = AgentRepository(db)
        self.knowledge_base_repo = KnowledgeBaseRepository(db)

    # ------------------------- Conversation 管理 -------------------------
    async def create_conversation(
        self, current_user: User, title: str
    ) -> Conversation:
        conversation = Conversation(user_id=current_user.id, title=title)
        return await self.conversation_repo.create(conversation)

    async def get_conversation(
        self, current_user: User, conversation_id: int
    ) -> Conversation:
        # 先查是否存在（不泄露其他用户内容）
        conversation = await self.conversation_repo.get_by_id(conversation_id)
        if conversation is None:
            raise ResourceNotFoundException(f"会话 ID={conversation_id} 不存在")

        # ownership 校验：非本人 → 403
        if conversation.user_id != current_user.id:
            raise PermissionDeniedException(
                "You can only access your own conversations",
            )
        return conversation

    async def list_conversations(
        self, current_user: User, page: int, page_size: int
    ) -> tuple[list[Conversation], int]:
        """分页列出当前用户的会话（稳定排序 created_at DESC, id DESC）。"""
        offset = (page - 1) * page_size
        items = await self.conversation_repo.list_by_user(
            current_user.id, offset=offset, limit=page_size
        )
        total = await self.conversation_repo.count_by_user(current_user.id)
        return items, total

    async def update_conversation_title(
        self, current_user: User, conversation_id: int, title: str
    ) -> Conversation:
        # ownership 校验（不存在 → 404；属于他人 → 403）
        conversation = await self.get_conversation(current_user, conversation_id)
        return await self.conversation_repo.update_title(conversation, title)

    async def delete_conversation(
        self, current_user: User, conversation_id: int
    ) -> None:
        conversation = await self.get_conversation(current_user, conversation_id)
        await self.conversation_repo.delete(conversation)

    async def list_messages(
        self, current_user: User, conversation_id: int
    ) -> list[Message]:
        """列出会话的历史消息（只读）。

        ownership 校验复用 :meth:`get_conversation`：
        会话不存在 → 404；会话属于他人 → 403（不泄露他人数据是否存在）。

        消息由 ``MessageRepository.list_by_conversation`` 返回，
        排序为 ``created_at ASC + id ASC``，与 :meth:`chat` 中传给
        Runtime 的上下文顺序完全一致，前端因此可以按原顺序渲染。

        说明：本阶段不引入消息分页——消息全部由 Agent 对话产生，
        单会话的消息量受交互规模约束；如后续需要，可按 Conversation
        的分页方式（page / page_size）在此扩展，不影响本接口的调用方。
        """
        await self.get_conversation(current_user, conversation_id)
        return await self.message_repo.list_by_conversation(conversation_id)

    # ------------------------------ Agent 对话 ------------------------------
    async def chat(
        self,
        current_user: User,
        conversation_id: int,
        message: str,
        agent_id: int | None = None,
    ) -> str:
        """执行一次 Agent 对话。

        Agent 配置（Stage 17）
        ---------------------
        指定 ``agent_id`` 时：

            读取 Agent（不存在 → 404）
              ↓ provider / model / temperature → 构建 Runtime（含工具子集）
              ↓ knowledge_base_id            → 构建该知识库的 Retriever
              ↓ system_prompt                → 与 RAG context 合并为唯一 system 消息

        未指定 ``agent_id`` 时完全沿用平台默认配置：Provider / Model /
        Temperature 取全局 ``settings``，工具为全部内置工具，
        **且不执行 RAG**（平台默认不绑定任何知识库，Stage 20）。

        Returns:
            str: LLM 生成的回答文本。

        Raises:
            ResourceNotFoundException: 会话或 Agent 不存在（404）。
            PermissionDeniedException: 会话不属于当前用户（403）。
            AppException: Agent 的 provider 等配置不合法（400）。
        """
        # 0. 解析执行配置：Agent 优先，未指定则用平台默认（Stage 20：默认不含 RAG）
        runtime = self.runtime
        # 平台 DI 注入的是 None（默认对话不检索知识库）；仅显式指定且绑定 KB 的 Agent 才检索
        retriever = self.retriever
        agent_prompt: str | None = None

        if agent_id is not None:
            agent = await self.get_agent(agent_id)
            # 防御：历史数据里可能存在非法 provider（正常写入路径已校验）
            self._validate_provider(agent.provider)

            agent_prompt = agent.system_prompt.strip() or None
            tool_names = await self.agent_repo.list_tool_names(agent.id)

            if self.runtime_factory is not None:
                runtime = self.runtime_factory(
                    provider=agent.provider,
                    model=agent.model or None,
                    temperature=agent.temperature,
                    tool_names=tool_names,
                )

            # 未绑定知识库 → 不检索（对话照常进行）
            retriever = None
            if agent.knowledge_base_id is not None:
                if self.retriever_factory is not None:
                    retriever = self.retriever_factory(agent.knowledge_base_id)
                else:  # pragma: no cover - DI 未注入工厂时的兜底
                    retriever = self.retriever

        # 1. ownership 校验（不存在 → 404；属于他人 → 403）
        conversation = await self.conversation_repo.get_by_id(conversation_id)
        if conversation is None:
            raise ResourceNotFoundException(f"会话 ID={conversation_id} 不存在")
        if conversation.user_id != current_user.id:
            raise PermissionDeniedException(
                "You can only access your own conversations",
            )

        # 2. 保存 user message
        user_msg = Message(
            conversation_id=conversation_id,
            role=MESSAGE_ROLE_USER,
            content=message,
        )
        await self.message_repo.create(user_msg)

        # 3. 查询全部历史（含本次 user message，按 created_at ASC, id ASC）
        history = await self.message_repo.list_by_conversation(conversation_id)

        # 4. 组装 messages 传给 Runtime（保证完整多轮上下文）
        messages = [
            {"role": m.role, "content": m.content} for m in history
        ]

        # 5. RAG：检索相关知识（可选增强；失败静默降级，不阻断对话）
        context = ""
        if retriever is not None:
            try:
                chunks = await retriever.retrieve(message)
                context = retriever.format_context(chunks)
            except Exception:  # noqa: BLE001
                context = ""

        # 6. 组装**唯一一条** system 消息（Agent 提示词 + RAG context）
        system_prompt = self._compose_system_prompt(agent_prompt, context)

        # 7. 调用 AgentRuntime（模型回答）
        reply = await runtime.run(messages, system_prompt=system_prompt)

        # 7. 保存 assistant message
        assistant_msg = Message(
            conversation_id=conversation_id,
            role=MESSAGE_ROLE_ASSISTANT,
            content=reply,
        )
        await self.message_repo.create(assistant_msg)

        return reply

    # ------------------------- Agent 配置管理 -------------------------
    @staticmethod
    def _validate_provider(provider: str) -> None:
        """校验 provider 是否受支持。

        复用 Provider 工厂的 ``SUPPORTED_PROVIDERS``（唯一来源），
        不在此处维护第二份 Provider 名单。

        Raises:
            AppException: provider 不在受支持列表中（HTTP 400）。
        """
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(SUPPORTED_PROVIDERS)
            raise AppException(
                f"不支持的 provider '{provider}'，当前支持: {supported}",
                status_code=400,
            )

    @staticmethod
    def _available_tool_names() -> list[str]:
        """当前已注册的工具名列表（唯一来源：内置 ToolRegistry）。

        用于校验 Agent 的工具绑定；**不允许绑定未注册工具**
        （因此不存在动态代码加载 / eval / exec 的可能）。
        """
        return [tool.name for tool in build_default_registry().list_tools()]

    @staticmethod
    def list_available_tools() -> list[Tool]:
        """列出可绑定给 Agent 的工具（供管理页面渲染选项）。

        与 ``_available_tool_names`` 同源（同一个内置 Registry），
        因此前端展示的可选项与后端校验的合法集合永远一致。
        """
        return build_default_registry().list_tools()

    def _validate_tool_names(self, tool_names: list[str]) -> None:
        """校验工具名是否都已注册（Stage 17）。

        Raises:
            AppException: 存在未注册的工具名（HTTP 400）。
        """
        allowed = self._available_tool_names()
        unknown = sorted({name for name in tool_names if name not in allowed})
        if unknown:
            raise AppException(
                f"不支持的工具: {', '.join(unknown)}；"
                f"当前可用工具: {', '.join(allowed)}",
                status_code=400,
            )

    async def _validate_knowledge_base(
        self, knowledge_base_id: int | None
    ) -> None:
        """校验绑定的知识库是否存在（``None`` 表示不绑定）。

        Raises:
            ResourceNotFoundException: 知识库不存在（HTTP 404）。
        """
        if knowledge_base_id is None:
            return
        knowledge_base = await self.knowledge_base_repo.get_by_id(
            knowledge_base_id
        )
        if knowledge_base is None:
            raise ResourceNotFoundException(
                f"知识库 ID={knowledge_base_id} 不存在"
            )

    @staticmethod
    def _compose_system_prompt(
        agent_prompt: str | None, context: str
    ) -> str | None:
        """组装 system prompt —— **只产生一条** system 消息（Stage 17）。

        - 无 RAG context：直接使用 Agent 提示词；Agent 未配置提示词时返回
          ``None``，由 Runtime 使用内置默认提示词（保持既有行为）；
        - 有 RAG context：把参考资料块追加在 Agent 提示词之后
          （不会出现「默认提示词 + Agent 提示词 + 重复提示词」的情况）。
        """
        if not context:
            return agent_prompt
        if agent_prompt:
            return (
                f"{agent_prompt}\n\n"
                f"{RAG_CONTEXT_INSTRUCTION.format(context=context)}"
            )
        return RAG_SYSTEM_PROMPT_TEMPLATE.format(context=context)

    async def _attach_tool_names(
        self, agents: list[Agent]
    ) -> list[Agent]:
        """把工具名列表挂到 ORM 对象的**非映射属性** ``tool_names`` 上。

        为什么不建 ORM relationship / 不在 Router 里查库：
        - 项目既有风格不使用 relationship（Repository 显式查询）；
        - Router 必须保持薄层（不查数据库）；
        该属性仅供 ``AgentResponse``（``from_attributes=True``）序列化使用，
        不参与持久化。
        """
        mapping = await self.agent_repo.list_tool_names_for(
            [agent.id for agent in agents]
        )
        for agent in agents:
            agent.tool_names = mapping.get(agent.id, [])
        return agents

    async def create_agent(self, data: AgentCreate) -> Agent:
        """创建 Agent 配置（含知识库绑定与工具绑定）。

        只保存配置，**不调用 LLM 验证 model**（避免 CRUD 与外部服务强耦合）。
        模型是否真实可用只有在对话执行时才能确认。
        """
        self._validate_provider(data.provider)
        self._validate_tool_names(data.tool_names)
        await self._validate_knowledge_base(data.knowledge_base_id)

        agent = Agent(
            name=data.name,
            description=data.description,
            system_prompt=data.system_prompt,
            provider=data.provider,
            model=data.model,
            temperature=data.temperature,
            knowledge_base_id=data.knowledge_base_id,
        )
        created = await self.agent_repo.create(agent)
        await self.agent_repo.set_tool_names(created.id, data.tool_names)
        await self._attach_tool_names([created])
        return created

    async def get_agent(self, agent_id: int) -> Agent:
        """按 ID 获取 Agent 配置（含工具绑定，供响应序列化）。

        Raises:
            ResourceNotFoundException: Agent 不存在（HTTP 404）。
        """
        agent = await self.agent_repo.get_by_id(agent_id)
        if agent is None:
            raise ResourceNotFoundException(f"Agent ID={agent_id} 不存在")
        await self._attach_tool_names([agent])
        return agent

    async def list_agents(self) -> list[Agent]:
        """列出全部 Agent 配置（按 created_at ASC, id ASC，含工具绑定）。"""
        agents = await self.agent_repo.list_all()
        return await self._attach_tool_names(agents)

    async def list_available_agents(self) -> list[Agent]:
        """列出可供普通用户选择的 Agent（Chat 页面用）。

        复用同一份数据；响应侧只暴露 id / name / description
        （见 ``AgentAvailableResponse``），不泄露 system_prompt 等管理配置。
        """
        return await self.agent_repo.list_all()

    async def update_agent(self, agent_id: int, data: AgentUpdate) -> Agent:
        """部分更新 Agent 配置（只更新显式传入的字段）。

        ``exclude_unset=True`` + 可空字段白名单：

        - 未传字段保持不变；
        - ``knowledge_base_id`` 允许显式传 ``null``（解绑知识库 → 不使用 RAG）；
        - 其余 NOT NULL 字段的 ``null`` 视为「不修改」（避免把列写成 NULL）。

        ``tool_names`` 属于关联表（``agent_tools``）数据，单独覆盖式重写。
        """
        agent = await self.get_agent(agent_id)

        raw = data.model_dump(exclude_unset=True)

        # 关联表数据单独处理（不是 agents 表字段）
        tool_names = raw.pop("tool_names", None)

        nullable_fields = {"knowledge_base_id"}
        update_data = {
            key: value
            for key, value in raw.items()
            if value is not None or key in nullable_fields
        }

        if "provider" in update_data:
            self._validate_provider(update_data["provider"])
        if "knowledge_base_id" in update_data:
            await self._validate_knowledge_base(
                update_data["knowledge_base_id"]
            )
        if tool_names is not None:
            self._validate_tool_names(tool_names)

        updated = await self.agent_repo.update(agent, **update_data)

        if tool_names is not None:
            await self.agent_repo.set_tool_names(updated.id, tool_names)

        await self._attach_tool_names([updated])
        return updated

    async def delete_agent(self, agent_id: int) -> None:
        """删除 Agent 配置。

        工具绑定（``agent_tools``）通过外键 ``ON DELETE CASCADE`` 自动清理；
        历史会话与消息不受影响（conversations / messages 不引用 agents）。

        Raises:
            ResourceNotFoundException: Agent 不存在（HTTP 404）。
        """
        agent = await self.get_agent(agent_id)
        await self.agent_repo.delete(agent)
