"""认证 Schema 定义。

定义登录请求/响应数据结构，与 ORM 模型分离，
确保 API 边界清晰、字段可控，不暴露任何敏感信息。
"""

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """登录请求体。

    用于 ``POST /api/auth/login`` 提交用户凭证。
    """

    email: EmailStr = Field(
        ...,
        max_length=100,
        description="用户注册邮箱",
        examples=["alice@example.com"],
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="用户明文密码，服务端验证后不保留",
        examples=["s3cret!"],
    )


class TokenResponse(BaseModel):
    """登录成功响应体。

    包含 JWT 访问令牌和令牌类型，用于后续 API 请求的 Authorization 头。
    """

    access_token: str = Field(
        ...,
        description="JWT 访问令牌（Bearer Token）",
    )
    token_type: str = Field(
        default="bearer",
        description="令牌类型，固定为 'bearer'",
        examples=["bearer"],
    )
