from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    """应用生命周期管理。

    启动时执行初始化逻辑，关闭时执行清理逻辑。
    暂不连接数据库、不初始化 LLM、不初始化 RAG。
    """
    print("Application starting...")
    yield
    print("Application shutting down...")
