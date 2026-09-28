"""认证 Service 层。

AuthService 负责登录业务流程编排：
根据邮箱查找用户 → 验证密码 → 签发 JWT。

只依赖 Repository（数据访问）和 security.py（密码/JWT 工具），
不依赖任何 HTTP 框架、AsyncSession 或数据库 Session。

与 UserService 的关系
----------------------
- **UserService**：管理用户生命周期（注册、更新资料、删除）。
  解决"这个用户是谁、他的信息是什么"的问题。
- **AuthService**：负责认证会话（登录、验证身份、签发令牌）。
  解决"当前请求者有没有权限、怎么证明身份"的问题。

两个 Service 职责正交，互不依赖：
- 注册用 UserService，登录用 AuthService。
- 修改密码时，UserService 处理"改了密码"的事实，
  AuthService 处理"新密码是否匹配、是否要作废旧令牌"。
"""

from app.core.exceptions import UnauthorizedException
from app.core.security import create_access_token, verify_password
from app.repositories.user import UserRepository
from app.schemas.auth import TokenResponse


class AuthService:
    """认证业务逻辑服务。

    通过构造函数接收 :class:`UserRepository`，
    不直接依赖 :class:`AsyncSession`，也不处理 HTTP 请求/响应。

    使用方式（在 Router 中）::

        from app.core.dependencies import get_db
        from app.repositories.user import UserRepository
        from app.services.auth import AuthService

        @router.post("/auth/login")
        async def login(
            data: LoginRequest,
            db: AsyncSession = Depends(get_db),
        ):
            repo = UserRepository(db)
            service = AuthService(repo)
            return await service.login(data.email, data.password)
    """

    def __init__(self, user_repository: UserRepository) -> None:
        """初始化 AuthService。

        Args:
            user_repository: 用户数据仓库实例。
                由调用方（Router）注入，AuthService 通过它查询用户，
                不持有 AsyncSession 引用。
        """
        self.user_repository = user_repository

    async def login(self, email: str, password: str) -> TokenResponse:
        """用户登录。

        完整流程：
        1. 通过 UserRepository 按邮箱查找用户。
        2. 用户不存在 → 抛出 UnauthorizedException（不区分"用户不存在"
           和"密码错误"，避免枚举攻击）。
        3. 调用 security.verify_password 验证明文密码与数据库哈希。
        4. 密码错误 → 抛出 UnauthorizedException（同上，错误消息完全一致）。
        5. 密码正确 → 调用 security.create_access_token 签发 JWT，
           payload 为 ``{"sub": str(user.id)}``。
        6. 返回 TokenResponse。

        Args:
            email: 用户登录邮箱。
            password: 用户输入的明文密码。

        Returns:
            TokenResponse: 包含 access_token 和 token_type。

        Raises:
            UnauthorizedException: 当邮箱不存在或密码错误时抛出。
        """
        # 1. 按邮箱查找用户
        user = await self.user_repository.get_by_email(email)

        # 2. 用户不存在
        if user is None:
            raise UnauthorizedException("Invalid email or password")

        # 3. 验证密码
        if not verify_password(password, user.hashed_password):
            # 4. 密码错误 — 使用完全相同的错误消息，防止用户名枚举
            raise UnauthorizedException("Invalid email or password")

        # 5. 签发 JWT
        token = create_access_token({"sub": str(user.id)})

        # 6. 返回令牌响应
        return TokenResponse(
            access_token=token,
            token_type="bearer",
        )
