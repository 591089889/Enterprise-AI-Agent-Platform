"""服务层（Service Layer）。

Service 层是业务逻辑的核心所在，位于 Router（HTTP 层）和 Repository（数据访问层）之间。

分层职责一览
------------

  Router（HTTP 层）
    ↓  解析 HTTP 请求，提取参数，调用 Service，返回 HTTP 响应
  Service（业务层）  ← 本模块
    ↓  编排业务逻辑，校验业务规则，调用一个或多个 Repository 方法
  Repository（数据访问层）
    ↓  封装 SQL 操作，不包含任何业务判断
  AsyncSession
    ↓  数据库会话，执行原生 SQL
  PostgreSQL
"""

from app.services.admin import AdminService
from app.services.auth import AuthService
from app.services.user import UserService

__all__ = ["AdminService", "AuthService", "UserService"]
