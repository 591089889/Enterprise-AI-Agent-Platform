"""用户 Schema 定义。

定义请求/响应数据结构，与 ORM 模型分离，
确保 API 边界清晰、字段可控。
"""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    """创建用户请求体。

    用于 ``POST /api/users`` 注册新用户。
    """

    username: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="用户名，长度 1-50，全局唯一",
        examples=["alice"],
    )
    email: EmailStr = Field(
        ...,
        max_length=100,
        description="邮箱地址，全局唯一",
        examples=["alice@example.com"],
    )
    password: str = Field(
        ...,
        min_length=6,
        max_length=128,
        description="明文密码，服务端不存储原文，哈希后入库",
        examples=["s3cret!"],
    )


class UserUpdate(BaseModel):
    """更新用户请求体。

    用于 ``PATCH /api/users/{id}`` 更新已有用户。
    所有字段均为可选，仅发送需要变更的字段即可。
    """

    username: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="新的用户名，长度 1-50",
        examples=["alice_updated"],
    )
    email: EmailStr | None = Field(
        default=None,
        max_length=100,
        description="新的邮箱地址",
        examples=["alice_new@example.com"],
    )


class UserRead(BaseModel):
    """用户响应体。

    用于 API 返回用户信息，**不暴露密码/哈希等敏感字段**。
    """

    id: int = Field(..., description="用户主键 ID")
    username: str = Field(..., description="用户名")
    email: EmailStr = Field(..., description="邮箱地址")
    role: str = Field(
        ...,
        description="用户角色：user / admin（前端据此做菜单与路由可见性判断）",
        examples=["user"],
    )
    created_at: datetime = Field(..., description="注册时间")

    model_config = {"from_attributes": True}
