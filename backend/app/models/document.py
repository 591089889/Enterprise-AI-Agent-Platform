from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# 索引状态常量（避免字符串散落在 Service / Router 中）
#: 尚未索引（新建文档、或内容修改后需要重新索引）
INDEX_STATUS_PENDING = "pending"
#: 已成功写入向量库
INDEX_STATUS_INDEXED = "indexed"
#: 索引失败（如 Embedding 配置缺失 / 向量库写入异常）
INDEX_STATUS_FAILED = "failed"


class Document(Base):
    """知识库文档 ORM 模型（原始文档）。

    对应数据库中的 ``documents`` 表。

    职责边界
    --------
    - 保存**原始文档正文**（``content``，不限于向量：企业知识库必须先有原文，
      才能重建索引、查看内容、审计来源）；
    - 与 ``knowledge_bases`` 是 1:N（删除知识库 → 级联删除其文档）；
    - 向量数据**不落在这里**：ChromaDB 继续负责 embedding / chunk 检索，
      本表只记录业务元数据与索引状态（``index_status``）。
    """

    __tablename__ = "documents"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 所属知识库 ID → knowledge_bases.id（删除知识库时级联删除文档）
    knowledge_base_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 文档名称：长度 200，非空
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # 原始文档正文（不限长度）
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 索引状态：pending / indexed / failed（见本模块常量）
    index_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=INDEX_STATUS_PENDING
    )

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    # 更新时间：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
