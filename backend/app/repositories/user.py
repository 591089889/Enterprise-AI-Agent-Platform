"""用户 Repository 层。

Repository（数据仓库）只负责数据库访问，不包含任何业务逻辑。
它是对 ``AsyncSession`` 的封装，将通用 SQL 操作翻译为语义化的方法调用。

核心概念解释
--------------

1. **AsyncSession 是什么？**

   ``AsyncSession`` 是 SQLAlchemy 提供的异步数据库会话对象。
   它代表一次与数据库的"对话"：
   - 管理 ORM 对象的**工作单元**（Unit of Work）：跟踪哪些对象被新增、修改、删除。
   - 管理**事务边界**：begin / commit / rollback。
   - 提供**异步查询接口**：``execute()``、``flush()``、``refresh()`` 等。
   - 在 FastAPI 中，每个 HTTP 请求通过 ``get_db`` 依赖获得一个独立的 AsyncSession，
     请求结束后自动关闭，避免会话泄漏。

2. **session.add() 做什么？**

   ``session.add(obj)`` 将 ORM 对象注册到 Session 的**暂存区**（identity map）。
   此时并**不发 SQL**，只是标记该对象"待插入"。真正的 INSERT 在：
   - ``flush()`` 时，或
   - ``commit()`` 时自动 flush。

3. **session.execute() 做什么？**

   ``session.execute(stmt)`` 将 SQLAlchemy 语句对象（如 ``select(User)``）编译为
   原生 SQL，发送到数据库执行，返回一个 ``Result`` 对象。这个 ``Result``
   包含行数据和元数据（列名、类型等），需要通过 ``scalar_one_or_none()``
   等方法提取具体值。

4. **select(User) 是什么？**

   ``select(User)`` 是 SQLAlchemy 2.0 的 **ORM SELECT 语句构造器**。
   它生成一个 ``Select`` 对象，等价于：
   ``SELECT users.id, users.username, users.email, users.created_at, users.updated_at FROM users``
   后续可以用 ``.where()`` / ``.order_by()`` / ``.limit()`` 等链式调用构造复杂查询。
   这种写法避免了手写 SQL 字符串，具有 IDE 提示和编译期类型检查能力。

5. **scalar_one_or_none() 是什么？**

   ``Result.scalar_one_or_none()`` 是提取查询结果的方法，行为如下：
   - 结果集有 **恰好 1 行** → 返回该行的第一列（通常是 ORM 实例）。
   - 结果集有 **0 行** → 返回 ``None``。
   - 结果集有 **多于 1 行** → 抛出 ``MultipleResultsFound`` 异常。
   适合主键/唯一键查询场景，语义精确、不会掩盖数据完整性问题。

6. **flush() 和 commit() 的区别？**

   =============  ========================================  ==============================================
   操作           做什么                                      数据是否对**其他**事务可见
   =============  ========================================  ==============================================
   ``flush()``    将暂存区的变更（INSERT/UPDATE/DELETE）         **不可见**（事务未提交，锁可能未释放）
                   编译为 SQL 发送到数据库，但不提交事务
   ``commit()``   提交当前事务，让数据库永久保存变更             **可见**
   =============  ========================================  ==============================================

   **Repository 使用 flush 而非 commit 的原因**：
   Repository 是数据访问层，不应控制事务边界。事务由上层（Service）
   决定何时开始和提交——一个 Service 方法可能调用多个 Repository 操作，
   这些操作应在同一个事务中原子完成。Repository 用 flush 保证数据库
   生成的值（自增 ID、默认时间戳）能立即回填到 ORM 对象中，
   而 COMMIT 交给外层统一控制。

7. **refresh() 是什么？**

   ``session.refresh(obj)`` 从数据库重新加载指定对象的所有列，
   覆盖 Python 对象中的当前值。在 flush 之后调用 refresh 可以确保：
   - 数据库生成的默认值（如 ``created_at``、``updated_at``）被同步回来。
   - 触发器中修改的字段被反映到 ORM 对象。
   - 对象状态与数据库完全一致。

8. **为什么 Repository 不应该自己负责业务逻辑？**

   将数据访问与业务逻辑分离是分层架构的核心原则，好处包括：

   - **单一职责**：Repository 只管"怎么存/取"，Service 管"什么时候该存/取/校验"。
   - **可测试性**：可以用 Mock Repository 测试 Service，用真实数据库测试 Repository。
   - **可复用性**：同一个 Repository 方法可被多个 Service 方法组合使用。
   - **变更隔离**：数据库表结构调整只影响 Repository，不波及业务层；
     业务规则变化只影响 Service，不波及数据访问层。
   - **清晰边界**：新人接手时能快速定位代码——"这个 bug 是查询写错了还是业务逻辑写错了？"
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """用户数据仓库。

    封装 ``users`` 表的所有数据库访问操作。
    每个方法都接收 :class:`AsyncSession` 并在其上下文中执行 SQL。

    使用方式（在 Service 中）::

        repo = UserRepository(session)
        user = await repo.get_by_id(1)
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化 UserRepository。

        Args:
            session: 异步数据库会话，由调用方通过 ``get_db`` 依赖注入。
                     每个 HTTP 请求持有一个独立会话，请求结束后自动关闭。
        """
        self.session = session

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    async def get_by_id(self, user_id: int) -> User | None:
        """按主键 ID 查询用户。

        使用 ``select(User)`` 构造查询 → ``execute()`` 发送到数据库 →
        ``scalar_one_or_none()`` 提取结果。

        Args:
            user_id: 用户主键 ID。

        Returns:
            匹配的 ``User`` ORM 对象；如果 ID 不存在则返回 ``None``。
        """
        # select(User)：生成 SELECT users.* FROM users
        # .where(User.id == user_id)：添加 WHERE 条件
        stmt = select(User).where(User.id == user_id)

        # execute(stmt)：将 SQL 语句发送到 PostgreSQL，返回 Result 对象
        # Result 包含行数据（Row 对象列表）和列元数据
        result = await self.session.execute(stmt)

        # scalar_one_or_none()：从结果中提取最多一个 ORM 实例
        # - 1 行 → 返回该 User 实例
        # - 0 行 → 返回 None
        # - >1 行 → 抛出 MultipleResultsFound（id 是主键，不会发生）
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        """按用户名查询用户。

        ``users`` 表的 ``username`` 列设有 UNIQUE 约束，
        因此查询结果只有 0 或 1 行。

        Args:
            username: 用户名。

        Returns:
            匹配的 ``User`` ORM 对象；如果用户名不存在则返回 ``None``。
        """
        stmt = select(User).where(User.username == username)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """按邮箱地址查询用户。

        ``users`` 表的 ``email`` 列设有 UNIQUE 约束，
        因此查询结果只有 0 或 1 行。

        Args:
            email: 邮箱地址。

        Returns:
            匹配的 ``User`` ORM 对象；如果邮箱不存在则返回 ``None``。
        """
        stmt = select(User).where(User.email == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # 分页查询方法
    # ------------------------------------------------------------------

    async def count_all(self) -> int:
        """统计用户总数。

        Returns:
            数据库中 users 表的总行数。
        """
        stmt = select(func.count()).select_from(User)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get_all_paginated(self, page: int, page_size: int) -> list[User]:
        """分页获取用户列表。

        按用户 ID 升序排列。

        Args:
            page: 页码（从 1 开始）。
            page_size: 每页数量。

        Returns:
            当前页的 ``User`` ORM 对象列表。
        """
        offset = (page - 1) * page_size
        stmt = (
            select(User)
            .order_by(User.id.asc())
            .offset(offset)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 写操作方法
    # ------------------------------------------------------------------

    async def create(self, user: User) -> User:
        """创建用户。

        注意：Repository **不负责密码哈希**。
        调用方应在传入 ``user`` 之前将明文密码替换为哈希值。

        流程：
        1. ``session.add(user)`` → 将 user 注册到暂存区，标记为待插入。
        2. ``flush()`` → 将 INSERT SQL 发送到数据库（但不 COMMIT）。
           数据库生成自增 ``id``、``created_at``、``updated_at``。
        3. ``refresh(user)`` → 从数据库重新加载该行，将 id 和
           时间戳同步回 Python 对象。

        Args:
            user: 待创建的用户 ORM 实例（此时 ``id``、``created_at``、
                  ``updated_at`` 可能为空）。

        Returns:
            已创建的 ``User`` ORM 实例（数据库生成的字段已回填）。
        """
        # add() 将对象注册到 Session 的 identity map，标记为 pending 状态
        self.session.add(user)

        # flush() 将 pending 状态的对象编译为 INSERT SQL 发送到数据库
        # 数据库端自增 id 和默认值（created_at）在此时生成
        await self.session.flush()

        # refresh() 从数据库重新 SELECT 该行，覆盖 Python 对象中的属性
        # 确保 user.id、user.created_at 等字段与数据库一致
        await self.session.refresh(user)
        return user

    async def update(self, user: User, **kwargs: object) -> User:
        """部分更新用户字段。

        通过 ``setattr`` 动态修改 ORM 对象的属性，
        然后 flush + refresh 将变更持久化并同步数据库生成的字段。

        注意：调用方应确保 ``user`` 已绑定到当前 Session
        （例如先通过 ``get_by_id`` 查询出来）。

        Args:
            user: 待更新的 ``User`` ORM 对象（已绑定到当前 session）。
            **kwargs: 需要更新的字段键值对，例如 ``username="new_name"``、
                      ``email="new@example.com"``。

        Returns:
            更新后的 ``User`` ORM 对象（``updated_at`` 已刷新）。

        Example::

            user = await repo.get_by_id(1)
            user = await repo.update(user, email="new@example.com")
        """
        # 动态设置属性 — 只更新 kwargs 中指定的字段，其余保持不变
        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)

        # flush() 将 UPDATE SQL 发送到数据库，触发 onupdate 默认值
        await self.session.flush()

        # refresh() 重新加载以获取数据库生成的 updated_at
        await self.session.refresh(user)
        return user

    async def delete(self, user: User) -> None:
        """删除用户。

        将 ORM 对象标记为待删除，flush 时发出 DELETE 语句。
        事务提交由外层（Service）统一控制。

        Args:
            user: 待删除的 ``User`` ORM 对象。
        """
        # delete() 将对象标记为 deleted 状态（不会立即发 SQL）
        await self.session.delete(user)

        # flush() 将 deleted 状态编译为 DELETE SQL 发送到数据库
        await self.session.flush()
