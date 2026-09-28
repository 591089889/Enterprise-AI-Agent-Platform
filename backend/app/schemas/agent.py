"""Agent Schema 定义。

包含两类模型（同一 Agent 领域，统一放在本文件）：

1. **Agent 对话**（``POST /agent/chat``）：AgentChatRequest / AgentChatResponse；
2. **Agent 配置管理**（``/agents``）：AgentCreate / AgentUpdate / AgentResponse。

字段风格与 ``schemas/conversation.py`` / ``schemas/user.py`` 保持一致：
显式字段声明 + ``Field(description=..., examples=...)``，
响应模型使用 ``model_config = {"from_attributes": True}``。

注意：``provider`` 的合法性由 Service 层复用
``app.agents.providers.SUPPORTED_PROVIDERS`` 校验（不在此处硬编码列表，
避免出现第二套 Provider 名单）。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """Agent 对话请求体。

    用于 ``POST /agent/chat`` 发送用户消息。
    必须指定会话 ID（conversation_id）。
    """

    conversation_id: int = Field(
        ...,
        ge=1,
        description="目标会话 ID",
        examples=[1],
    )
    agent_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "使用的 Agent 配置 ID（可选）。\n\n"
            "- 指定时：使用该 Agent 的 provider / model / temperature / "
            "system_prompt / 知识库 / 工具绑定执行；\n"
            "- 未指定时：沿用平台默认配置（backend/.env 中的全局设置）。"
        ),
        examples=[1],
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="用户输入消息",
        examples=["你好"],
    )


class AgentChatResponse(BaseModel):
    """Agent 对话响应体。

    包含会话 ID 与 LLM 生成的回答文本。
    """

    conversation_id: int = Field(
        ...,
        description="会话 ID",
        examples=[1],
    )
    reply: str = Field(
        ...,
        description="Agent 生成的回答",
        examples=["你好！我是你的 AI 助手。"],
    )


# ------------------------------------------------------------------
# Agent 配置管理（/agents）
# ------------------------------------------------------------------
class AgentCreate(BaseModel):
    """创建 Agent 配置请求体。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Agent 名称",
        examples=["客服助手"],
    )
    description: str = Field(
        default="",
        max_length=500,
        description="Agent 描述（可为空）",
        examples=["负责处理企业售后问题"],
    )
    system_prompt: str = Field(
        default="",
        max_length=4000,
        description="系统提示词（可为空，为空时由 Runtime 使用内置提示词）",
        examples=["你是一名企业售后客服助手，请用简洁专业的中文回答。"],
    )
    provider: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="LLM Provider（合法值见后端 SUPPORTED_PROVIDERS）",
        examples=["qwen"],
    )
    model: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="模型名称",
        examples=["qwen3-8b"],
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="生成温度（0.0 - 2.0）",
        examples=[0.7],
    )
    knowledge_base_id: int | None = Field(
        default=None,
        ge=1,
        description="绑定的知识库 ID（0 或 1 个；为空表示不使用 RAG）",
        examples=[1],
    )
    tool_names: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "允许该 Agent 使用的工具名称列表（必须是后端已注册的工具，"
            "如 calculator / datetime）；为空表示不向 LLM 暴露任何工具"
        ),
        examples=[["calculator"]],
    )


class AgentUpdate(BaseModel):
    """更新 Agent 配置请求体（所有字段可选，只更新传入的字段）。"""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="新的 Agent 名称",
        examples=["客服助手 v2"],
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="新的描述",
        examples=["负责处理企业售后与退款问题"],
    )
    system_prompt: str | None = Field(
        default=None,
        max_length=4000,
        description="新的系统提示词",
        examples=["你是企业售后专家。"],
    )
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="新的 Provider",
        examples=["ollama"],
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="新的模型名",
        examples=["qwen3-8b"],
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="新的生成温度",
        examples=[0.3],
    )
    knowledge_base_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "新的知识库绑定；显式传 null 表示解绑（不使用 RAG），"
            "不传则保持不变"
        ),
        examples=[1],
    )
    tool_names: list[str] | None = Field(
        default=None,
        max_length=20,
        description="新的工具绑定列表（覆盖式；传 [] 表示清空）",
        examples=[["calculator", "datetime"]],
    )


class AgentResponse(BaseModel):
    """Agent 配置响应体（管理 API 的读模型）。"""

    id: int = Field(..., description="Agent ID")
    name: str = Field(..., description="Agent 名称")
    description: str = Field(..., description="描述")
    system_prompt: str = Field(..., description="系统提示词")
    provider: str = Field(..., description="LLM Provider")
    model: str = Field(..., description="模型名称")
    temperature: float = Field(..., description="生成温度")
    knowledge_base_id: int | None = Field(
        default=None,
        description="绑定的知识库 ID（null 表示不使用 RAG）",
    )
    tool_names: list[str] = Field(
        default_factory=list,
        description="该 Agent 可用的工具名称列表（空表示不暴露工具）",
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


class AgentAvailableResponse(BaseModel):
    """给普通用户（Chat 页面）的 Agent 选择项。

    最小暴露面：只返回 id / name / description，
    **不返回** system_prompt / provider / model / temperature 等管理配置。
    """

    id: int = Field(..., description="Agent ID", examples=[1])
    name: str = Field(..., description="Agent 名称", examples=["客服助手"])
    description: str = Field(
        default="", description="Agent 描述", examples=["负责处理企业售后问题"]
    )

    model_config = {"from_attributes": True}


class AgentToolOption(BaseModel):
    """可绑定给 Agent 的工具选项（唯一来源：内置 ToolRegistry）。

    管理页面用它渲染工具多选框，避免前端硬编码第二份工具名单。
    """

    name: str = Field(..., description="工具名称", examples=["calculator"])
    description: str = Field(
        ..., description="工具说明（供 LLM 判断何时调用）"
    )

    model_config = {"from_attributes": True}


__all__ = [
    "AgentAvailableResponse",
    "AgentChatRequest",
    "AgentChatResponse",
    "AgentCreate",
    "AgentResponse",
    "AgentToolOption",
    "AgentUpdate",
]

