from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


class MessageRepository:
    """消息数据仓库。只查询/flush，不 commit。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_all(self) -> int:
        """统计消息总数（Dashboard 概览使用，Stage 18）。

        Returns:
            数据库中 messages 表的总行数。
        """
        stmt = select(func.count()).select_from(Message)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def create(self, message: Message) -> Message:
        self.session.add(message)
        await self.session.flush()
        await self.session.refresh(message)
        return message

    async def list_by_conversation(
        self, conversation_id: int
    ) -> list[Message]:
        """按会话列出消息，created_at ASC + id ASC 稳定排序。"""
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
