from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Agent(Base):
    """Agent 配置 ORM 模型。

    对应数据库中的 ``agents`` 表。

    职责边界
    --------
    本表只保存 **Agent 的配置**（名称、描述、系统提示词、Provider、
    模型名、温度），不保存运行态数据：

    - 一次对话的落库消息属于 ``messages``，迭代/工具调用等运行态只存在于
      Runtime 的内存对象中（不落库）；
    - 会话归属属于 ``conversations``；
    - 工具绑定属于 ``agent_tools``（关联表，只存已注册工具的名称）。

    配置如何生效（Stage 17）
    ------------------------
    ``provider`` / ``model`` / ``temperature`` / ``system_prompt`` /
    ``knowledge_base_id`` / ``agent_tools`` 由 ``AgentService.chat()`` 读取，
    用于构建该次对话的 Provider、Runtime（含工具子集）与 RAG 检索器。
    Runtime **不感知**本模型（它只接收 messages 与 system_prompt）。
    """

    __tablename__ = "agents"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Agent 名称：长度 100，非空
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Agent 描述：长度 500，非空，默认空字符串
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )

    # 系统提示词：长度 4000，非空，默认空字符串
    # （与 messages.content 保持同一长度上限，避免提示词长度风格不一致）
    system_prompt: Mapped[str] = mapped_column(
        String(4000), nullable=False, default=""
    )

    # LLM Provider 名称（如 qwen / ollama），长度 20，非空
    # 合法值由 Service 层复用 app.agents.providers.SUPPORTED_PROVIDERS 校验
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="qwen"
    )

    # 模型名（如 qwen3-8b），长度 100，非空
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    # 生成温度：0.0 - 2.0（范围由 Pydantic Schema 校验）
    temperature: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.7
    )

    # 绑定的知识库（0 或 1 个）：NULL 表示不使用 RAG
    # ON DELETE SET NULL：删除知识库时自动解除绑定，避免留下悬空引用
    # 导致后续 Chat 随机 500（该 Agent 退化为「无知识库」正常对话）
    knowledge_base_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    # 更新时间：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
