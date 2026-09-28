"""Document Schema 定义（Stage 16）。

列表与详情的读模型分开：

- :class:`DocumentSummary` —— 列表项，**不含 content**（正文可能很大，
  列表接口不返回大字段）；
- :class:`DocumentDetail` —— 详情，包含完整 ``content``。

``index_status`` 取值：``pending`` / ``indexed`` / ``failed``
（常量见 ``app.models.document``）。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    """创建文档请求体（保存原始正文，不做切分/向量化）。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="文档名称",
        examples=["售后政策.md"],
    )
    content: str = Field(
        ...,
        min_length=1,
        description="原始文档正文（不限长度，索引时由 RAG 切分）",
        examples=["本政策适用于……"],
    )


class DocumentUpdate(BaseModel):
    """更新文档请求体（所有字段可选，只更新传入的字段）。

    修改 ``content`` 后索引会失效：Service 会把 ``index_status``
    重置为 ``pending``，需要管理员重新触发索引。
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="新的文档名称",
        examples=["售后政策 v2.md"],
    )
    content: str | None = Field(
        default=None,
        min_length=1,
        description="新的文档正文",
        examples=["本政策适用于……（已更新）"],
    )


class DocumentSummary(BaseModel):
    """文档列表项（不含 content）。"""

    id: int = Field(..., description="文档 ID")
    knowledge_base_id: int = Field(..., description="所属知识库 ID")
    name: str = Field(..., description="文档名称")
    index_status: str = Field(
        ...,
        description="索引状态：pending / indexed / failed",
        examples=["pending"],
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


class DocumentDetail(BaseModel):
    """文档详情（含完整正文）。"""

    id: int = Field(..., description="文档 ID")
    knowledge_base_id: int = Field(..., description="所属知识库 ID")
    name: str = Field(..., description="文档名称")
    content: str = Field(..., description="原始文档正文")
    index_status: str = Field(
        ...,
        description="索引状态：pending / indexed / failed",
        examples=["indexed"],
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


class DocumentIndexResponse(BaseModel):
    """索引操作结果。

    ``status`` 是**唯一真源**，绝不谎报成功：

    - ``indexed``：已成功写入向量库；
    - ``failed`` ：索引失败（``message`` 说明面向用户的原因）。
    """

    document_id: int = Field(..., description="文档 ID", examples=[1])
    status: str = Field(
        ...,
        description="索引状态：indexed / failed",
        examples=["indexed"],
    )
    message: str = Field(
        default="",
        description="失败时的用户可读说明（成功时为空字符串）",
        examples=[""],
    )


__all__ = [
    "DocumentCreate",
    "DocumentDetail",
    "DocumentIndexResponse",
    "DocumentSummary",
    "DocumentUpdate",
]
