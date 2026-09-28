"""用户 API Router。

Router（路由层）的职责定位
--------------------------

**Router 是什么？**

Router 是 FastAPI 的 HTTP 入口层，负责将 HTTP 请求映射到业务方法。
它定义 URL 路径、HTTP 方法、请求参数、响应模型和状态码。

**Router 的职责：**

1. **定义 HTTP 接口**：路径（``/users``）、方法（GET/POST/PATCH/DELETE）。
2. **解析请求参数**：路径参数（``{user_id}``）、请求体（``UserCreate``）。
3. **调用 Service 层**：通过 ``Depends()`` 注入 ``UserService``，调用其业务方法。
4. **处理 HTTP 响应**：设置 ``response_model``、``status_code``、``HTTPException``。

**为什么 Router 不直接操作数据库？**

1. **单一职责原则**：Router 是 HTTP 层，只应关心"怎么接收、怎么返回"。
   如果 Router 中写 ``session.execute(select(User))``，就混淆了表示层和
   数据访问层的边界——改一个 SQL 查询可能要改 Router，改一个返回格式
   可能要改 Repository，维护成本飙升。

2. **测试复杂度**：Router 的单元测试应该用 Mock Service 来验证 HTTP 行为。
   如果 Router 直接操作数据库，测试就必须启动真实数据库，变成集成测试。

3. **复用性为 0**：Router 中的数据库代码无法被 CLI 脚本、定时任务、
   消息队列消费者复用——因为它和 HTTP 上下文绑死。

**Depends() 如何注入 Service？**

FastAPI 的依赖注入系统通过 ``Depends()`` 自动构建依赖链：

  Router
    ↓  ``Depends(get_user_service)``
  get_user_service()
    ↓  从 ``get_db`` 获取 ``AsyncSession``
    ↓  创建 ``UserRepository(session)``
    ↓  创建 ``UserService(repository)``
    ↓  返回给 Router

整个过程对 Router 透明——Router 只需要声明参数类型，
FastAPI 自动完成注入和生命周期管理。

**response_model 的作用：**

``response_model`` 告诉 FastAPI：
- 使用哪个 Pydantic 模型来序列化返回值。
- 自动过滤掉不在模型中定义的字段（如 ``hashed_password``）。
- 自动生成 OpenAPI 文档中的响应 schema。
- 对返回数据进行校验，确保 API 不泄露敏感字段。

**HTTPException 为什么放在 Router？**

``HTTPException`` 是 FastAPI 特定的异常类，它的语义是：
"这个 HTTP 请求应该返回什么状态码和错误信息"。
这纯粹是 HTTP 层的问题，与业务逻辑无关。

如果 Service 抛出 ``HTTPException``：
- Service 就被绑定到 FastAPI 框架，无法在非 HTTP 场景复用。
- CLI 脚本调用 Service 时，会收到无缘无故的 HTTP 错误。

正确的做法：
- Service 抛出通用异常（如 ``ValueError``、自定义业务异常）。
- Router 捕获这些异常，转换为对应的 ``HTTPException``。

依赖注入链路图
--------------

完整的请求流程图：::

    HTTP Request (POST /users)
      ↓
    Router (users.py)
      ↓  Depends(get_user_service)  # FastAPI 自动注入
    get_user_service()              # dependencies.py
      ↓  Depends(get_db)            # 获取 AsyncSession
    UserRepository(session)         # 创建仓库
      ↓
    UserService(repository)         # 创建服务
      ↓
    Router.create_user(data, service)
      ↓  service.create_user(data)  # 调用业务方法
    Service 层执行业务逻辑
      ↓  repo.get_by_username(data.username)  # 检查唯一性
      ↓  repo.get_by_email(data.email)
      ↓  User(...) 构造 ORM 对象
      ↓  repo.create(user)           # 写入数据库
      ↓
    Router 返回 HTTP 201 + UserRead
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.dependencies import get_current_user, get_user_service
from app.core.exceptions import AppException, PermissionDeniedException
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services.user import UserService

# ------------------------------------------------------------------
# APIRouter 实例
# ------------------------------------------------------------------
# prefix="/users"：所有路由的 URL 前缀统一为 /users
# tags=["users"]：在 OpenAPI 文档中归入 "users" 分组
router = APIRouter(
    prefix="/users",
    tags=["users"],
)


# ------------------------------------------------------------------
# 创建用户
# ------------------------------------------------------------------
@router.post(
    "/",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建用户",
    description="注册新用户。用户名和邮箱必须全局唯一。",
)
async def create_user(
    # Request Body：由 FastAPI 自动从 POST body 中解析 UserCreate
    data: UserCreate,
    # 依赖注入：FastAPI 自动调用 get_user_service()，
    # 构建 UserRepository → UserService 的完整依赖链
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserRead:
    """创建新用户。

    流程：
    1. 接收 UserCreate（username、email、password）。
    2. 注入 UserService（通过 Depends 构建完整依赖链）。
    3. 调用 service.create_user() 执行业务逻辑。
    4. 返回序列化后的 UserRead（不包含 password）。

    注意：
    - Router 不直接访问数据库。
    - Router 不检查用户名/邮箱唯一性（Service 负责）。
    - Router 不处理密码哈希（Service 负责）。
    - response_model=UserRead 确保不泄露敏感字段。
    """
    try:
        # 调用 Service 层执行业务逻辑
        # Router 只负责"调用"，不关心"怎么实现"
        user = await service.create_user(data)
        return user
    except AppException as exc:
        # Service 层抛出自定义业务异常（如 BusinessConflictException）
        # Router 将其统一映射为 HTTP 响应：
        #   exc.status_code → HTTP 状态码
        #   exc.message     → HTTP 响应体 detail 字段
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )


# ------------------------------------------------------------------
# 获取当前登录用户
# ------------------------------------------------------------------
@router.get(
    "/me",
    response_model=UserRead,
    summary="获取当前登录用户",
    description=("返回当前已认证用户的个人信息。\n\n"
                 "**需要登录**：必须在 Authorization header 中提供有效的 Bearer Token。"),
)
async def get_current_user_info(
    # 依赖注入：Depends(get_current_user) 会自动完成以下步骤：
    # 1. 提取 Authorization: Bearer <token>
    # 2. jwt.decode() 验证签名和过期
    # 3. 从 payload.sub 提取 user_id
    # 4. 通过 UserRepository 查询用户
    # 5. 返回 User ORM 对象（或 401）
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserRead:
    """获取当前登录用户信息。

    不需要 user_id 参数——用户 ID 由 JWT 中的 ``sub`` 字段提供。

    流程：
    1. FastAPI 调用 ``get_current_user()`` 完成认证。
    2. 得到当前登录的 ``User`` ORM 对象。
    3. Router 直接返回该对象（FastAPI 通过 ``response_model=UserRead``
       自动过滤掉 ``hashed_password`` 等敏感字段）。
    4. 如果 token 无效/过期/用户不存在，``get_current_user()``
       内部会抛出 HTTP 401，不会到达本函数。

    注意：
    - Router 不调用 jwt.decode()。
    - Router 不创建 UserRepository。
    - Router 不直接操作数据库。
    - Router 只做一件事：把认证过的 User 返回为 UserRead。
    """
    # current_user 已经是认证过的 User ORM 对象。
    # FastAPI 的 response_model=UserRead 会自动：
    # - 过滤掉 hashed_password（不在 UserRead 中）
    # - 过滤掉 updated_at（不在 UserRead 中）
    # - 只返回 id, username, email, created_at
    return current_user


# ------------------------------------------------------------------
# 获取用户
# ------------------------------------------------------------------
@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="获取用户",
    description=(
        "根据用户 ID 获取用户信息。\n\n"
        "**权限（Stage 18 最终权限审计修正）**：需要登录，且只能查看**自己**的账号；"
        "查看他人账号返回 403。管理员查看任意用户请使用 ``GET /admin/users/{user_id}``。\n\n"
        "修正原因：该接口此前**没有任何认证依赖**——任何未登录请求都能读取任意用户的 "
        "username / email / role，与本项目「未登录访问受保护 API → 401」以及"
        "「普通用户不得读取其他用户的敏感信息」两条权限规范冲突（审计项 §13）。"
    ),
)
async def get_user(
    # 路径参数：从 URL 路径中提取 user_id
    user_id: int,
    # 依赖注入：认证（未登录 → 401）
    current_user: Annotated[User, Depends(get_current_user)],
    # 依赖注入：FastAPI 自动构建 UserService
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserRead:
    """获取用户信息（仅限本人；管理员请走 ``/admin/users/{id}``）。

    认证与授权流程（顺序与 ``GET /conversations/{id}`` 保持一致）：

    1. ``Depends(get_current_user)`` 完成认证（未登录 401）；
    2. ``service.get_user(user_id)`` 查询（不存在 → 404）；
    3. ``current_user.id == user_id`` 授权判断（他人 → 403）；
    4. 返回 ``UserRead``（不含 hashed_password）。
    """
    # 先判存在性（与 conversations 既有约定一致：不存在 → 404）
    user = await service.get_user(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 ID={user_id} 不存在",
        )

    # 授权：只能查看自己的账号（他人 → 403，不返回任何用户字段）
    if current_user.id != user_id:
        raise PermissionDeniedException(
            "You can only view your own account",
        )

    return user


# ------------------------------------------------------------------
# 更新用户
# ------------------------------------------------------------------
@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="更新用户",
    description=(
        "部分更新用户信息。只更新请求体中传入的字段。\n\n"
        "**需要登录**：必须提供有效的 Bearer Token。\n\n"
        "**权限限制**：只能修改自己的账户，不能修改其他用户。"
    ),
)
async def update_user(
    # 路径参数
    user_id: int,
    # 请求体：只更新传入的字段
    data: UserUpdate,
    # 依赖注入：Service 层
    service: Annotated[UserService, Depends(get_user_service)],
    # 依赖注入：当前登录用户（Authentication）
    # get_current_user() 负责：提取 Bearer Token → jwt.decode() → 查询 User
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserRead:
    """更新用户信息。

    认证与授权流程：
    1. Depends(get_current_user) 完成身份认证（Authentication）。
       验证 JWT → 得到 current_user（"你是谁"）。
    2. 比较 current_user.id == user_id（Authorization 授权判断）。
       如果 ID 不匹配 → HTTP 403 Forbidden（"你没有权限"）。
    3. 通过 service.get_user(user_id) 获取目标用户。
    4. 如果用户不存在 → HTTP 404。
    5. 调用 service.update_user(user, data) 部分更新。
    6. 返回更新后的 UserRead。

    注意：
    - Router 先"认证" → 再"授权" → 再"执行业务"。
    - 认证失败 → 401（由 get_current_user 负责）。
    - 授权失败 → 403（由本函数负责）。
    - Router 不直接操作数据库。
    - Router 不调用 jwt.decode()。
    """
    # ------------------------------------------------------------------
    # Authorization（授权）：
    # 当前登录用户只能修改自己的账户。
    # current_user 已由 Depends(get_current_user) 完成认证。
    # ------------------------------------------------------------------
    if current_user.id != user_id:
        raise PermissionDeniedException(
            "You can only update your own account",
        )

    # 先检查目标用户是否存在（此时 user_id 已确认等于 current_user.id）
    existing_user = await service.get_user(user_id)
    if existing_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 ID={user_id} 不存在，无法更新",
        )

    try:
        # 委托 Service 执行部分更新
        updated_user = await service.update_user(existing_user, data)
        return updated_user
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )


# ------------------------------------------------------------------
# 删除用户
# ------------------------------------------------------------------
@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除用户",
    description=(
        "根据用户 ID 删除用户。\n\n"
        "**需要登录**：必须提供有效的 Bearer Token。\n\n"
        "**权限限制**：只能删除自己的账户，不能删除其他用户。"
    ),
)
async def delete_user(
    # 路径参数
    user_id: int,
    # 依赖注入：Service 层
    service: Annotated[UserService, Depends(get_user_service)],
    # 依赖注入：当前登录用户（Authentication）
    # get_current_user() 负责：提取 Bearer Token → jwt.decode() → 查询 User
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """删除用户。

    认证与授权流程：
    1. Depends(get_current_user) 完成身份认证（Authentication）。
       验证 JWT → 得到 current_user（"你是谁"）。
    2. 比较 current_user.id == user_id（Authorization 授权判断）。
       如果 ID 不匹配 → HTTP 403 Forbidden（"你没有权限"）。
    3. 通过 service.get_user(user_id) 获取目标用户。
    4. 如果用户不存在 → HTTP 404。
    5. 调用 service.delete_user(user) 删除。
    6. 返回 204 No Content。

    注意：
    - Router 先"认证" → 再"授权" → 再"执行业务"。
    - 认证失败 → 401（由 get_current_user 负责）。
    - 授权失败 → 403（由本函数负责）。
    - 204 表示操作成功但响应体为空。
    """
    # ------------------------------------------------------------------
    # Authorization（授权）：
    # 当前登录用户只能删除自己的账户。
    # current_user 已由 Depends(get_current_user) 完成认证。
    # ------------------------------------------------------------------
    if current_user.id != user_id:
        raise PermissionDeniedException(
            "You can only delete your own account",
        )

    # 先检查目标用户是否存在（此时 user_id 已确认等于 current_user.id）
    existing_user = await service.get_user(user_id)
    if existing_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 ID={user_id} 不存在，无法删除",
        )

    # 委托 Service 执行删除
    await service.delete_user(existing_user)

    # 204 No Content：不需要 return 任何内容
    # FastAPI 会在 status_code=204 时自动跳过响应体
