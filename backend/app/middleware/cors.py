from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings


def setup_cors(app: FastAPI):
    """配置 CORS 中间件。

    开发环境允许前端本地开发服务器访问。
    不使用 allow_origins=["*"]，因为不能携带凭证且无法精确控制来源。
    """
    if settings.ENVIRONMENT == "development":
        # Enterprise 前端开发地址固定为 5174
        # （5173 属于另一个项目 Travel-H5，不作为本项目的允许来源）
        allow_origins = [
            "http://localhost:5174",
        ]
    else:
        allow_origins = []

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
