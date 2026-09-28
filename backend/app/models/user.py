from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class User(Base):
    """用户 ORM 模型。

    对应数据库中的 ``users`` 表。
    使用 SQLAlchemy 2.0 推荐的 ``Mapped`` + ``mapped_column`` 声明式写法。
    """

    __tablename__ = "users"

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 用户名：长度 50，非空，唯一
    username: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True)

    # 邮箱：长度 100，非空，唯一
    email: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True)

    # 密码哈希：长度 255，非空，服务端永远只存哈希值
    hashed_password: Mapped[str] = mapped_column(
        String(255), nullable=False)

    # 角色：长度 20，非空，默认为 "user"
    # 可选值：user / admin（见 app.core.roles）
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user")

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    # 更新时间：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
