"""Conversation Schema 定义。

定义会话创建/响应数据结构，遵循项目 Pydantic 风格。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    """创建会话请求体。title 可选。"""

    title: str = Field(
        default="",
        max_length=200,
        description="会话标题（可选）",
        examples=["我的会话"],
    )


class ConversationUpdate(BaseModel):
    """重命名会话请求体。只允许修改 title。"""

    title: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="新的会话标题",
        examples=["我的旅行计划"],
    )


class ConversationResponse(BaseModel):
    """会话响应体。"""

    id: int = Field(..., description="会话 ID")
    user_id: int = Field(..., description="所属用户 ID")
    title: str = Field(..., description="会话标题")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


class PaginatedConversationsResponse(BaseModel):
    """会话分页响应体（遵循项目 PaginatedUsersResponse 风格）。"""

    items: list[ConversationResponse] = Field(
        ...,
        description="当前页的会话列表",
    )
    total: int = Field(
        ...,
        description="会话总数",
        examples=[100],
    )
    page: int = Field(
        ...,
        description="当前页码（从 1 开始）",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        description="每页数量",
        examples=[20],
    )
