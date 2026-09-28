from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Conversation(Base):
    """会话 ORM 模型。

    对应数据库中的 ``conversations`` 表。
    一个用户（User）拥有多个会话（1:N）。
    """

    __tablename__ = "conversations"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 所有者用户 ID → users.id
    # 删除用户时级联删除其所有会话
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 会话标题：长度 200，可空，默认空字符串
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default=""
    )

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    # 更新时间：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
