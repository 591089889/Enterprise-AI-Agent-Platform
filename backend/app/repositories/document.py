"""Document Repository 层。

职责边界
--------
只封装 ``documents`` 表的数据库访问，不含业务逻辑：

- 不校验 ``knowledge_base_id`` 是否存在（Service 层）；
- 不控制事务：只 ``flush()`` / ``refresh()``。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document


class DocumentRepository:
    """知识库文档数据仓库。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_all(self) -> int:
        """统计文档总数（Dashboard 概览使用，Stage 18）。

        Returns:
            数据库中 documents 表的总行数。
        """
        stmt = select(func.count()).select_from(Document)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def create(self, document: Document) -> Document:
        """创建文档（flush + refresh，不 commit）。"""
        self.session.add(document)
        await self.session.flush()
        await self.session.refresh(document)
        return document

    async def get_by_id(self, document_id: int) -> Document | None:
        """按 ID 查询文档（不含 knowledge_base 归属过滤，供 Service 区分 404）。"""
        stmt = select(Document).where(Document.id == document_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_and_knowledge_base(
        self, document_id: int, knowledge_base_id: int
    ) -> Document | None:
        """按 ID + 所属知识库查询（用于资源关系校验，避免跨库访问）。"""
        stmt = select(Document).where(
            Document.id == document_id,
            Document.knowledge_base_id == knowledge_base_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_knowledge_base(
        self, knowledge_base_id: int
    ) -> list[Document]:
        """列出某个知识库的全部文档（``created_at ASC, id ASC``）。"""
        stmt = (
            select(Document)
            .where(Document.knowledge_base_id == knowledge_base_id)
            .order_by(Document.created_at.asc(), Document.id.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, document: Document, **kwargs: object) -> Document:
        """部分更新文档字段（flush + refresh，不 commit）。"""
        for key, value in kwargs.items():
            if hasattr(document, key):
                setattr(document, key, value)
        await self.session.flush()
        await self.session.refresh(document)
        return document

    async def delete(self, document: Document) -> None:
        """删除文档（delete + flush，不 commit）。"""
        await self.session.delete(document)
        await self.session.flush()
