from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class KnowledgeBase(Base):
    """知识库 ORM 模型。

    对应数据库中的 ``knowledge_bases`` 表。

    定位
    ----
    **平台级资源（platform-level resource）**：由管理员统一管理，
    因此没有 ``user_id`` / ``owner_id`` / ``tenant_id``（不引入多租户）。

    与 RAG 的关系
    -------------
    本表只保存知识库的**业务元数据**；向量数据仍然只存在于
    ChromaDB（每个知识库一个 collection，见
    ``KnowledgeBaseService._collection_name``）。

        knowledge_bases
              │ 1:N
              ▼
        documents（原始文档正文）
              │ 索引时切分
              ▼
        ChromaDB collection（向量 / chunk）
    """

    __tablename__ = "knowledge_bases"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 知识库名称：长度 100，非空
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 知识库描述：长度 500，非空，默认空字符串
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    # 更新时间：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
