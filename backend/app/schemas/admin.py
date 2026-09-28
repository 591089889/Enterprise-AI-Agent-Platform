"""管理员 Schema 定义。

定义管理员 API 的请求/响应数据结构。
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.user import UserRead


class AdminUserRoleUpdate(BaseModel):
    """管理员修改用户角色请求体。

    使用 ``Literal["user", "admin"]`` 确保 Pydantic 在请求进入时
    就拒绝非法角色值，不需要在 Service 中再次校验。
    """

    role: Literal["user", "admin"] = Field(
        ...,
        description="目标角色：user 或 admin",
        examples=["admin"],
    )


class DashboardStats(BaseModel):
    """平台概览统计（``GET /admin/dashboard``，Stage 18）。

    所有数字都来自真实数据库（``SELECT COUNT(*)``），**不做任何估算或伪造**。
    """

    users: int = Field(..., ge=0, description="用户总数", examples=[3])
    agents: int = Field(..., ge=0, description="Agent 总数", examples=[5])
    knowledge_bases: int = Field(
        ..., ge=0, description="知识库总数", examples=[2]
    )
    documents: int = Field(..., ge=0, description="文档总数", examples=[12])
    conversations: int = Field(
        ..., ge=0, description="会话总数", examples=[35]
    )
    messages: int = Field(..., ge=0, description="消息总数", examples=[246])


class PaginatedUsersResponse(BaseModel):
    """分页用户列表响应体。

    用于 ``GET /admin/users`` 返回分页数据。
    """

    items: list[UserRead] = Field(
        ...,
        description="当前页的用户列表",
    )
    total: int = Field(
        ...,
        description="用户总数",
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
