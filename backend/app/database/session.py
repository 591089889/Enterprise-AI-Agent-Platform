"""数据库会话与**统一事务边界**。

本模块是整个后端唯一负责 ``commit()`` / ``rollback()`` 的地方：
Repository 只做 ``flush()`` / ``refresh()``，Service 只编排业务，
Router 只处理 HTTP —— 它们都不控制事务。

调用约定（重要）
----------------

``get_db`` 必须以 ``Depends(get_db, scope="function")`` 声明
（项目内统一使用 ``app.core.dependencies.DbSession`` 别名）：

- ``scope="function"``：FastAPI 在 endpoint 返回后、**响应字节发出之前**
  关闭该依赖，因此 ``commit()`` 一定先于响应完成；
- 默认 ``scope="request"``：FastAPI 在 ``await response(...)`` **之后**
  才关闭依赖，``commit()`` 会晚于响应发出，导致
  「写接口返回 201，但紧接着的读请求看不到数据」的可见性竞态。

两种时序（见 ``fastapi.routing.request_response``）::

    scope="function"（本项目）        scope="request"（默认，有问题）
    ----------------------------      --------------------------------
    handler → flush                   handler → flush
    关闭依赖 → commit                 响应发出        ← 客户端先拿到 200/201
    响应发出                          关闭依赖 → commit

"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# 异步数据库引擎
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# 异步会话工厂
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供请求级异步数据库会话，并管理事务边界。

    Session 生命周期
    ----------------

    每个 HTTP 请求通过 ``Depends(get_db, scope="function")``
    （即 ``app.core.dependencies.DbSession``）获得一个独立 Session，
    请求结束后由 ``async_session()`` 上下文管理器自动关闭。

    事务 commit / rollback 边界
    ---------------------------

    - 请求处理成功（Router/Service 正常返回）→ ``session.commit()`` 提交；
    - 请求处理过程中抛出异常 → ``session.rollback()`` 回滚，避免脏事务；
    - ``commit()`` / ``rollback()`` 只在这里由数据库层统一负责，
      Service 和 Repository 不自行控制事务边界。

    执行时机（重要）
    ----------------

    以 ``Depends(get_db, scope="function")`` 声明时，本生成器 ``yield``
    之后的代码在 **endpoint 返回之后、响应字节发出之前** 执行，因此：

        写入数据库 → commit → 响应返回给客户端

    即客户端收到 201/200 时，数据已经提交并立即可见（新连接也能读到）。
    若使用 FastAPI 默认的 ``scope="request"``，``commit()`` 会晚于响应
    发出，出现「写接口成功但紧接着读取不到」的竞态 —— 因此本项目的
    ``DbSession`` 别名固定使用 ``function`` 作用域。

    关于读取请求
    ------------

    ``session.commit()`` 在没有待提交变更时是 no-op：
    - 不会发出任何 SQL；
    - 只是结束当前事务。
    因此读取请求不会产生不必要的 commit。

    Raises
    ------
    Exception
        任何在请求处理中抛出的异常都会先触发 rollback，再原样向上抛出。
    """
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
