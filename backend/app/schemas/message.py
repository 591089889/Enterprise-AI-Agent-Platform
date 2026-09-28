"""Message Schema 定义。

定义会话历史消息的**只读**响应数据结构，供前端加载历史消息使用：

    GET /conversations/{conversation_id}/messages

设计说明
--------

- 本模块只提供读模型（``MessageResponse``），**不提供写模型**：
  消息的写入仍然只发生在 ``AgentService.chat()``（用户消息 + AI 回复
  在一次事务内落库），API 层不开放"直接写消息"的入口。
- 与 ``ConversationResponse`` 保持一致的风格：字段显式声明 + 
  ``model_config = {"from_attributes": True}``（直接由 ORM 对象校验）。

角色取值由 ``app.models.message`` 中的常量约束：

- ``MESSAGE_ROLE_USER``      = ``"user"``
- ``MESSAGE_ROLE_ASSISTANT`` = ``"assistant"``
"""

from datetime import datetime

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    """消息响应体（历史消息只读视图）。"""

    id: int = Field(..., description="消息 ID")
    conversation_id: int = Field(..., description="所属会话 ID")
    role: str = Field(
        ...,
        description="消息角色：user（用户）/ assistant（AI）",
        examples=["user"],
    )
    content: str = Field(
        ...,
        description="消息内容",
        examples=["你好"],
    )
    created_at: datetime = Field(..., description="创建时间")

    model_config = {"from_attributes": True}


__all__ = ["MessageResponse"]
