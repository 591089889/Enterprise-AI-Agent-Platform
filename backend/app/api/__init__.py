"""API 路由层（Router Layer）。

Router 是 HTTP 请求的入口，负责将 HTTP 请求映射到 Service 层的业务方法。

分层职责
--------

  HTTP 请求
    ↓
  Router（表示层） ← 本模块
    ↓  解析请求参数、调用 Service、格式化 HTTP 响应
  Service（业务层）
    ↓  业务流程编排、业务规则校验
  Repository（数据访问层）
    ↓  数据库 CRUD 操作
  AsyncSession → PostgreSQL

Router 的职责
-------------

- **解析 HTTP 请求**：从路径参数、查询参数、请求体中提取数据。
- **调用 Service**：将解析后的数据传递给 Service 方法。
- **返回 HTTP 响应**：设置 status_code、response_model、HTTPException。
- **不直接操作数据库**：Router 不应调用 ``session.execute()`` 或 ``select()``。

Router 不负责
-------------

- SQL 查询 / 数据库操作 → 这是 Repository 的职责
- 业务规则判断 → 这是 Service 的职责
- 密码哈希 / 数据校验 → 这是 Service 的职责
"""

__all__ = []
