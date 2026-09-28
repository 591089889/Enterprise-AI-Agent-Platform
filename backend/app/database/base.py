from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    基于 SQLAlchemy 2.0 DeclarativeBase，用于统一管理元数据。
    """
    pass
