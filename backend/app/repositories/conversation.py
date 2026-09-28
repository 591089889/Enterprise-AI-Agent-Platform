from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation


class ConversationRepository:
    """会话数据仓库。

    封装 ``conversations`` 表的所有数据库访问操作。
    遵循 UserRepository 风格：只执行查询/flush，**不提交事务**。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, conversation: Conversation) -> Conversation:
        """创建会话（flush + refresh，不 commit）。"""
        self.session.add(conversation)
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation

    async def get_by_id(self, conversation_id: int) -> Conversation | None:
        """按 ID 查询会话（不含 ownership 过滤，供权限区分）。"""
        stmt = select(Conversation).where(Conversation.id == conversation_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_and_user(
        self, conversation_id: int, user_id: int
    ) -> Conversation | None:
        """按 ID + 用户查询会话（ownership 查询）。"""
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self, user_id: int, offset: int, limit: int
    ) -> list[Conversation]:
        """按用户分页列出会话。

        稳定排序：created_at DESC, id DESC。
        分页由 PostgreSQL 的 OFFSET/LIMIT 完成，不做内存切片。
        """
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_user(self, user_id: int) -> int:
        """统计该用户的会话总数。"""
        stmt = (
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.user_id == user_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_all(self) -> int:
        """统计会话总数（Dashboard 概览使用，Stage 18）。

        Returns:
            数据库中 conversations 表的总行数。
        """
        stmt = select(func.count()).select_from(Conversation)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def update_title(
        self, conversation: Conversation, title: str
    ) -> Conversation:
        """更新会话标题（flush + refresh，不 commit）。"""
        conversation.title = title
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation

    async def delete(self, conversation: Conversation) -> None:
        """删除会话（delete + flush，不 commit）。"""
        await self.session.delete(conversation)
        await self.session.flush()
