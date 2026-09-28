"""KnowledgeBase Repository 层。

职责边界
--------
只封装 ``knowledge_bases`` 表的数据库访问，不含业务逻辑：

- 不校验业务规则（Service 层）；
- 不控制事务：只 ``flush()`` / ``refresh()``，
  ``commit()`` / ``rollback()`` 由 ``app.database.session.get_db`` 统一负责。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase


class KnowledgeBaseRepository:
    """知识库数据仓库。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_all(self) -> int:
        """统计知识库总数（Dashboard 概览使用，Stage 18）。

        Returns:
            数据库中 knowledge_bases 表的总行数。
        """
        stmt = select(func.count()).select_from(KnowledgeBase)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def create(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        """创建知识库（flush + refresh，不 commit）。"""
        self.session.add(knowledge_base)
        await self.session.flush()
        await self.session.refresh(knowledge_base)
        return knowledge_base

    async def get_by_id(self, knowledge_base_id: int) -> KnowledgeBase | None:
        """按 ID 查询知识库（不存在返回 None）。"""
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[KnowledgeBase]:
        """列出全部知识库（``created_at ASC, id ASC``）。

        知识库是平台级管理资源、数量规模小，因此不做分页
        （与 ``AgentRepository.list_all`` 保持一致）。
        """
        stmt = select(KnowledgeBase).order_by(
            KnowledgeBase.created_at.asc(), KnowledgeBase.id.asc()
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self, knowledge_base: KnowledgeBase, **kwargs: object
    ) -> KnowledgeBase:
        """部分更新知识库字段（flush + refresh，不 commit）。"""
        for key, value in kwargs.items():
            if hasattr(knowledge_base, key):
                setattr(knowledge_base, key, value)
        await self.session.flush()
        await self.session.refresh(knowledge_base)
        return knowledge_base

    async def delete(self, knowledge_base: KnowledgeBase) -> None:
        """删除知识库（delete + flush，不 commit）。

        其下的 ``documents`` 由数据库外键 ``ON DELETE CASCADE`` 一并删除。
        """
        await self.session.delete(knowledge_base)
        await self.session.flush()
