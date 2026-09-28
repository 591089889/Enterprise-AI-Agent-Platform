"""认证 API Router。

Auth Router 负责将 HTTP 登录请求映射到 AuthService 的 login 方法。
不直接操作数据库、不调用 security.py、不持有 Repository。

职责边界
--------

  HTTP Request (POST /auth/login)
    ↓
  Auth Router (auth.py)       ← 本模块
    ↓  Depends(get_auth_service)
  AuthService.login()         ← Service 层
    ↓  verify_password() + create_access_token()
    ↓  UserRepository.get_by_email()
    ↓
  HTTP 200 + TokenResponse
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.dependencies import get_auth_service
from app.core.exceptions import AppException
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.auth import AuthService

# ------------------------------------------------------------------
# APIRouter 实例
# ------------------------------------------------------------------
# prefix="/auth"：所有认证路由的 URL 前缀统一为 /auth
# 最终访问路径：/auth/login
# tags=["auth"]：在 OpenAPI 文档中归入 "auth" 分组
router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


# ------------------------------------------------------------------
# 登录
# ------------------------------------------------------------------
@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="用户登录",
    description=(
        "使用邮箱和密码登录，返回 JWT 访问令牌。\n\n"
        "**安全设计**：无论用户不存在还是密码错误，均返回相同的 401 错误消息，"
        "防止用户名枚举攻击。"
    ),
)
async def login(
    # Request Body：由 FastAPI 自动从 POST body 中解析 LoginRequest
    data: LoginRequest,
    # 依赖注入：FastAPI 自动调用 get_auth_service()，
    # 构建 UserRepository → AuthService 的完整依赖链
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """用户登录。

    流程：
    1. 接收 LoginRequest（email、password）。
    2. 注入 AuthService（通过 Depends 构建完整依赖链）。
    3. 调用 service.login(email, password) 执行业务逻辑。
    4. 返回 TokenResponse（access_token + token_type）。

    异常处理：
    - AuthService 抛出 ValueError → Router 转换为 HTTP 401。
    - 错误消息统一为 "Invalid email or password"，不区分原因。

    注意：
    - Router 不直接访问数据库。
    - Router 不调用 verify_password() 或 create_access_token()。
    - Router 不持有 UserRepository 引用。
    - 所有业务逻辑交给 AuthService。
    """
    try:
        # 委托 AuthService 执行完整的登录流程
        # Router 只负责"接收请求 → 调用 Service → 返回响应"
        return await service.login(
            email=data.email,
            password=data.password,
        )
    except AppException as exc:
        # AuthService 在以下情况抛出 UnauthorizedException（统一消息）：
        # - 邮箱不存在
        # - 密码不匹配
        # Router 将其统一映射为 HTTP 响应：
        #   exc.status_code=401 → HTTP 401
        #   exc.message → detail
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )
