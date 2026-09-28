from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.agent import router as agent_router
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.knowledge_bases import router as knowledge_bases_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.lifespan import lifespan
from app.middleware.cors import setup_cors

app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
)

setup_cors(app)

# ------------------------------------------------------------------
# 注册全局异常处理器（Stage 10：统一错误响应）
# ------------------------------------------------------------------
# 所有异常（业务异常 / HTTPException / 参数校验 / 未预期异常）
# 都由此收敛为统一 JSON 结构：
#   {"error": {"code": "...", "message": "..."}}
register_exception_handlers(app)

# ------------------------------------------------------------------
# 注册 API 路由
# ------------------------------------------------------------------
# 管理员相关接口：/admin/*
app.include_router(admin_router)
# Agent 相关接口：/agent/*
app.include_router(agent_router)
# Agent 配置管理接口：/agents/*（管理员权限）
app.include_router(agents_router)
# 认证相关接口：/auth/*
app.include_router(auth_router)
# 会话相关接口：/conversations/*
app.include_router(conversations_router)
# 知识库管理接口：/knowledge-bases/*（管理员权限，Stage 16）
app.include_router(knowledge_bases_router)
# 用户相关接口：/users/*
app.include_router(users_router)


@app.get("/")
async def root():
    return {"message": "Enterprise AI Agent Platform API"}
