"""用户 Service 层。

Service（业务逻辑层）的职责和边界
---------------------------------

**Service 是什么？**

Service 是业务逻辑的核心所在。它位于 Router（HTTP 层）和
Repository（数据访问层）之间，负责编排业务流程、校验业务规则，
但不直接与数据库通信。

**为什么 Service 不直接使用 AsyncSession？**

1. **职责分离**：AsyncSession 是数据库层面的概念（连接、事务、
   SQL 执行），而 Service 负责的是"业务规则"（用户名不能重复、
   邮箱不能重复、密码需要哈希）。将两者混在一起会导致：
   - 业务逻辑和数据库操作纠缠，改一个字段要翻遍整个 Service。
   - 单元测试时不得不 mock 整个数据库会话，而不仅仅是 mock
     数据访问方法。

2. **可替换性**：如果未来需要切换数据源（例如从 PostgreSQL
   换成其他存储），只需替换 Repository 实现，Service 代码
   完全不用变——因为 Service 只依赖 Repository 的接口。

3. **关注点聚焦**：Service 的方法读起来像是在描述业务流程：
   "检查用户名是否存在 → 检查邮箱是否存在 → 哈希密码 →
   创建用户"。如果其中穿插了 session.execute() 等底层调用，
   业务流程的清晰度会被破坏。

**为什么 Service 通过 Repository 操作数据库？**

Repository 将数据库操作封装成语义化的方法（get_by_id、
get_by_username、create、update、delete），Service 调用
这些方法就像调用一个"数据访问接口"，不需要知道底层是
SELECT 还是 INSERT，也不需要知道 flush/refresh 的细节。
这符合"依赖倒置原则"——高层模块（Service）不依赖低层模块
（SQLAlchemy Session），两者都依赖抽象（Repository 方法签名）。

**业务逻辑应该放在哪里？**

- **Service**：业务规则、校验逻辑、流程编排。
  例如："创建用户前必须检查用户名和邮箱的唯一性"——
  这是一个业务规则，放在 Service。
- **Repository**：数据如何存取、查询如何构造。
  例如："按用户名查询的 SQL 语句怎么写"——
  这是数据访问细节，放在 Repository。
- **Router**：HTTP 请求解析、参数提取、响应格式化。
  例如："从 POST body 中提取 UserCreate JSON"——
  这是 HTTP 层面的工作，放在 Router。

**Service 为什么不处理 HTTP？**

Service 是纯 Python 业务逻辑，不依赖任何 Web 框架。
它不导入 FastAPI 的 Depends、HTTPException、Request 等。
这意味着：
- Service 可以在非 HTTP 上下文中复用（例如 CLI 脚本、
  消息队列消费者、定时任务）。
- 单元测试不需要启动 HTTP 服务器，直接实例化 Service 即可。
- 如果未来从 FastAPI 迁移到其他框架，Service 代码零改动。
"""

from app.core.exceptions import BusinessConflictException
from app.core.security import password_hash
from app.models.user import User
from app.repositories.user import UserRepository
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    """用户业务逻辑服务。

    通过构造函数接收 :class:`UserRepository`，
    不直接依赖 :class:`AsyncSession`，也不处理 HTTP 请求/响应。

    使用方式（在 Router 中）::

        from app.core.dependencies import get_db
        from app.repositories.user import UserRepository
        from app.services.user import UserService

        @router.post("/users")
        async def create_user(
            data: UserCreate,
            db: AsyncSession = Depends(get_db),
        ):
            repo = UserRepository(db)
            service = UserService(repo)
            return await service.create_user(data)
    """

    def __init__(self, user_repository: UserRepository) -> None:
        """初始化 UserService。

        Args:
            user_repository: 用户数据仓库实例。
                由调用方（Router）注入，每个 HTTP 请求持有独立的
                Repository 实例，共享同一个 AsyncSession。
        """
        # Service 只持有 Repository 引用，不持有 AsyncSession。
        # 所有数据库操作都委托给 Repository，Service 只负责
        # 业务逻辑判断和流程编排。
        self.user_repository = user_repository

    # ------------------------------------------------------------------
    # 业务方法
    # ------------------------------------------------------------------

    async def create_user(self, data: UserCreate) -> User:
        """创建新用户。

        业务流程：
        1. 检查用户名是否已被占用
        2. 检查邮箱是否已被占用
        3. 调用 :func:`password_hash` 对明文密码进行 bcrypt 哈希
        4. 构造 User ORM 对象（``hashed_password`` 存储哈希值）
        5. 通过 Repository 写入数据库

        Args:
            data: 创建用户请求体（包含 username、email、password）。

        Returns:
            已创建的 ``User`` ORM 对象（id、created_at 等字段已回填）。

        Raises:
            BusinessConflictException: 当用户名或邮箱已被占用时抛出。
        """
        # 1. 检查用户名唯一性
        existing_username = await self.user_repository.get_by_username(
            data.username
        )
        if existing_username is not None:
            raise BusinessConflictException(f"用户名 '{data.username}' 已被占用")

        # 2. 检查邮箱唯一性
        existing_email = await self.user_repository.get_by_email(data.email)
        if existing_email is not None:
            raise BusinessConflictException(f"邮箱 '{data.email}' 已被注册")

        # 3. 密码哈希
        # 调用 security.py 中的 password_hash()，将明文密码转换为 bcrypt 哈希。
        # 明文密码 data.password 只在内存中短暂存在，不会进入数据库或日志。
        hashed = password_hash(data.password)

        # 4. 构造 User ORM 对象
        # hashed_password 存储 bcrypt 哈希值，数据库永远不存明文密码。
        user = User(
            username=data.username,
            email=data.email,
            hashed_password=hashed,
        )

        # 5. 通过 Repository 写入数据库
        # Repository.create() 负责 add → flush → refresh，
        # Service 不接触 session，只关心"创建用户"这个操作的结果。
        created_user = await self.user_repository.create(user)

        return created_user

    async def get_user(self, user_id: int) -> User | None:
        """根据用户 ID 获取用户。

        纯粹委托给 Repository，无额外业务逻辑。
        返回 ``None`` 表示用户不存在，由调用方（Router）决定
        如何处理（例如返回 404 响应）。

        Args:
            user_id: 用户主键 ID。

        Returns:
            匹配的 ``User`` ORM 对象；不存在则返回 ``None``。
        """
        # 直接委托给 Repository 的查询方法。
        # Service 不添加额外逻辑，保持查询路径的清晰。
        return await self.user_repository.get_by_id(user_id)

    async def update_user(self, user: User, data: UserUpdate) -> User:
        """更新用户信息。

        只更新 ``UserUpdate`` 中实际传入（非 None）的字段。
        更新通过 Repository.update() 执行，该方法使用
        ``setattr`` 动态设置属性，然后 flush + refresh。

        Args:
            user: 待更新的 ``User`` ORM 对象（应已绑定到当前 Session，
                  通常由 ``get_user()`` 查询获得）。
            data: 更新请求体，所有字段均为可选。

        Returns:
            更新后的 ``User`` ORM 对象（updated_at 已刷新）。
        """
        # 将 Pydantic Schema 转换为 dict，过滤掉值为 None 的字段。
        # exclude_unset=True 确保只包含用户显式传入的字段，
        # 未传入的字段不会出现在 dict 中，因此不会被覆盖为 None。
        update_data = data.model_dump(exclude_unset=True)

        # 委托给 Repository 执行部分更新。
        # Repository 使用 **kwargs 展开 update_data，
        # 通过 setattr 只修改对应字段。
        updated_user = await self.user_repository.update(user, **update_data)

        return updated_user

    async def delete_user(self, user: User) -> None:
        """删除用户。

        委托给 Repository.delete() 执行。
        Repository 负责 session.delete() → flush()，
        实际的 COMMIT 由外层（调用方 Router 的 get_db 上下文）
        统一控制。

        Args:
            user: 待删除的 ``User`` ORM 对象。
        """
        await self.user_repository.delete(user)
