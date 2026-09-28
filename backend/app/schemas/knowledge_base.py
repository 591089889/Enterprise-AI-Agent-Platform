"""Knowledge Base Schema 定义（Stage 16）。

字段风格与 ``schemas/agent.py`` / ``schemas/conversation.py`` 保持一致：
显式字段声明 + ``Field(description=..., examples=...)``，
响应模型使用 ``model_config = {"from_attributes": True}``。

知识库是**平台级资源**，因此请求体里没有 user_id / tenant_id。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    """创建知识库请求体。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="知识库名称",
        examples=["企业产品手册"],
    )
    description: str = Field(
        default="",
        max_length=500,
        description="知识库描述（可为空）",
        examples=["存放产品说明与售后政策"],
    )


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库请求体（所有字段可选，只更新传入的字段）。"""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="新的知识库名称",
        examples=["产品手册 v2"],
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="新的描述",
        examples=["产品说明 + 售后政策 + 常见问题"],
    )


class KnowledgeBaseResponse(BaseModel):
    """知识库响应体。"""

    id: int = Field(..., description="知识库 ID")
    name: str = Field(..., description="知识库名称")
    description: str = Field(..., description="知识库描述")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


__all__ = [
    "KnowledgeBaseCreate",
    "KnowledgeBaseResponse",
    "KnowledgeBaseUpdate",
]
