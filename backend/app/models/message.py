from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Message(Base):
    """消息 ORM 模型。

    对应数据库中的 ``messages`` 表。
    一个会话（Conversation）拥有多条消息（1:N）。
    """

    __tablename__ = "messages"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 所属会话 ID → conversations.id
    # 删除会话时级联删除其所有消息
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 消息角色：user / assistant
    # 第一阶段只允许用户消息与助手消息
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    # 消息内容
    content: Mapped[str] = mapped_column(
        String(4000), nullable=False
    )

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )


# 消息角色常量（第一阶段仅 user / assistant）
MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"
