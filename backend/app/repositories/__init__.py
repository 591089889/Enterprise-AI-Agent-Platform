"""Repository 层统一导出。

所有数据访问对象均从此处导入。
"""

from app.repositories.user import UserRepository

__all__ = [
    "UserRepository",
]
