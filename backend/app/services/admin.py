"""管理员 Service 层。

AdminService 负责管理员业务逻辑编排：

1. 用户管理：用户列表、查看用户、修改角色、删除用户（Stage 5）；
2. 平台概览统计：``get_dashboard_stats()``（Stage 18 Dashboard）。

只依赖 Repository（数据访问），不依赖任何 HTTP 框架、AsyncSession 或
数据库 Session。权限检查由 ``require_admin()`` 在 Router 层完成。

事务边界：本 Service **不 commit / rollback**，由 ``get_db`` 统一负责。
"""

from app.core.exceptions import (
    PermissionDeniedException,
    ResourceNotFoundException,
)
from app.models.user import User
from app.repositories.agent import AgentRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.document import DocumentRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository


class AdminService:
    """管理员业务逻辑服务。

    通过构造函数接收各表的 Repository，不直接依赖 :class:`AsyncSession`，
    也不处理 HTTP 请求/响应。

    使用方式（在 Router 中）::

        from app.core.dependencies import get_admin_service, require_admin

        @router.get("/admin/users")
        async def list_users(
            admin: Annotated[User, Depends(require_admin)],
            service: Annotated[AdminService, Depends(get_admin_service)],
        ):
            return await service.list_users(page=1, page_size=20)
    """

    def __init__(
        self,
        user_repository: UserRepository,
        agent_repository: AgentRepository,
        conversation_repository: ConversationRepository,
        message_repository: MessageRepository,
        knowledge_base_repository: KnowledgeBaseRepository,
        document_repository: DocumentRepository,
    ) -> None:
        """初始化 AdminService。

        Args:
            user_repository: 用户数据仓库。
            agent_repository: Agent 配置仓库（Dashboard 统计）。
            conversation_repository: 会话仓库（Dashboard 统计）。
            message_repository: 消息仓库（Dashboard 统计）。
            knowledge_base_repository: 知识库仓库（Dashboard 统计）。
            document_repository: 文档仓库（Dashboard 统计）。
        """
        self.user_repository = user_repository
        self.agent_repository = agent_repository
        self.conversation_repository = conversation_repository
        self.message_repository = message_repository
        self.knowledge_base_repository = knowledge_base_repository
        self.document_repository = document_repository

    # ------------------------------------------------------------------
    # 管理员业务方法
    # ------------------------------------------------------------------

    async def list_users(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[User], int]:
        """获取分页用户列表。

        Args:
            page: 页码（从 1 开始）。
            page_size: 每页数量（1~100）。

        Returns:
            (用户列表, 总数) 元组。
        """
        total = await self.user_repository.count_all()
        users = await self.user_repository.get_all_paginated(page, page_size)
        return users, total

    async def get_user(self, user_id: int) -> User:
        """管理员查看指定用户。

        Args:
            user_id: 目标用户 ID。

        Returns:
            目标用户的 ORM 对象。

        Raises:
            ResourceNotFoundException: 用户不存在。
        """
        user = await self.user_repository.get_by_id(user_id)
        if user is None:
            raise ResourceNotFoundException(f"用户 ID={user_id} 不存在")
        return user

    async def update_user_role(
        self, user_id: int, role: str, admin_id: int
    ) -> User:
        """管理员修改用户角色。

        安全规则：
        - 不允许管理员将自己的角色改为 "user"（避免丧失管理员权限）。
        - 角色值由 Pydantic Schema（``Literal["user", "admin"]``）
          在 Router 层完成校验，本方法信任传入值合法。

        Args:
            user_id: 目标用户 ID。
            role: 新角色（"user" 或 "admin"）。
            admin_id: 执行操作的管理员用户 ID。

        Returns:
            更新后的用户 ORM 对象。

        Raises:
            ResourceNotFoundException: 目标用户不存在。
            PermissionDeniedException: 管理员尝试降级自己。
        """
        user = await self.user_repository.get_by_id(user_id)
        if user is None:
            raise ResourceNotFoundException(f"用户 ID={user_id} 不存在")

        if user_id == admin_id:
            raise PermissionDeniedException(
                "Administrators cannot change their own role",
            )

        updated_user = await self.user_repository.update(user, role=role)
        return updated_user

    async def delete_user(self, user_id: int, admin_id: int) -> None:
        """管理员删除用户。

        安全规则：管理员不能删除自己。

        Args:
            user_id: 目标用户 ID。
            admin_id: 执行操作的管理员用户 ID。

        Raises:
            ResourceNotFoundException: 目标用户不存在。
            PermissionDeniedException: 管理员尝试删除自己。
        """
        user = await self.user_repository.get_by_id(user_id)
        if user is None:
            raise ResourceNotFoundException(f"用户 ID={user_id} 不存在")

        if user_id == admin_id:
            raise PermissionDeniedException(
                "Administrators cannot delete themselves",
            )

        await self.user_repository.delete(user)

    # ------------------------------------------------------------------
    # 平台概览统计（Dashboard，Stage 18）
    # ------------------------------------------------------------------

    async def get_dashboard_stats(self) -> dict[str, int]:
        """统计平台核心资源数量（全部来自真实数据库）。

        Returns:
            包含 6 个计数的字典（键与 ``DashboardStats`` 字段一致）：
            ``users`` / ``agents`` / ``knowledge_bases`` / ``documents`` /
            ``conversations`` / ``messages``。

        说明
        ----
        - 每个数字都是对应表的 ``SELECT COUNT(*)``，不做估算、不伪造；
        - 6 次查询**顺序执行**（同一个 AsyncSession 不支持并发语句），
          在 PostgreSQL 上开销可忽略，因此不引入缓存 / Redis / 物化视图；
        - 不返回无法从数据库可靠获取的指标（如 CPU / QPS）。
        """
        return {
            "users": await self.user_repository.count_all(),
            "agents": await self.agent_repository.count_all(),
            "knowledge_bases": (
                await self.knowledge_base_repository.count_all()
            ),
            "documents": await self.document_repository.count_all(),
            "conversations": await self.conversation_repository.count_all(),
            "messages": await self.message_repository.count_all(),
        }
