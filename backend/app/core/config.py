from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置类，从 .env 文件和环境变量读取配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    APP_NAME: str = "Enterprise AI Agent Platform"
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen"
    # LLM Base URL：留空时使用各 Provider 内置默认端点
    LLM_BASE_URL: str = ""
    # LLM Provider 选择：qwen（默认）或 ollama
    LLM_PROVIDER: str = "qwen"
    # LLM 生成温度（0.0 - 2.0），由 Provider 工厂透传给具体 Provider
    LLM_TEMPERATURE: float = 0.7

    # JWT / 认证配置
    SECRET_KEY: str = "change-this-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # --------------------------- Agent / Tool ---------------------------
    # 是否启用 Agent Tool Calling（LLM 可根据 Tool Schema 调用工具）。
    # 关闭后 Agent 退化为纯文本对话，行为与引入 Tool 之前完全一致。
    AGENT_TOOL_CALLING_ENABLED: bool = True

    # ------------------------- RAG / Knowledge Base -------------------------
    # 本地知识文档目录（相对 backend/ 或绝对路径）
    KNOWLEDGE_DIR: str = "knowledge"
    # ChromaDB 持久化目录（相对 backend/ 或绝对路径）
    VECTOR_STORE_DIR: str = ".chroma"
    # ChromaDB Collection 名称
    VECTOR_COLLECTION_NAME: str = "knowledge_chunks"
    # Embedding Provider 选择：
    #   - hash      本地确定性散列嵌入（仅开发/链路验证，不注入真实 RAG context）
    #   - ollama    本地 Ollama Embedding（需要 ollama 服务运行）
    #   - dashscope DashScope Text Embedding（需要 EMBEDDING_API_KEY）
    EMBEDDING_PROVIDER: str = "hash"
    # Embedding 模型名（dashscope: text-embedding-v4；ollama: nomic-embed-text 等）
    EMBEDDING_MODEL: str = ""
    # DashScope Embedding API Key（EMBEDDING_PROVIDER=dashscope 时必填）
    EMBEDDING_API_KEY: str = ""
    # Embedding Base URL（EMBEDDING_PROVIDER=ollama 时使用，默认 http://localhost:11434）
    EMBEDDING_BASE_URL: str = ""
    # 检索返回 Top K 知识片段
    RAG_TOP_K: int = 4
    # 文本切分参数
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    # 知识库文件上传（Stage 22）：**单个文件**大小上限（MB）。
    # 项目不保存原始文件（无 OSS / MinIO / uploads 目录），但仍必须限制上传体积，
    # 超过上限由 KnowledgeBaseService 在写入临时文件时立即中止（HTTP 413）。
    MAX_UPLOAD_FILE_SIZE_MB: int = 10


settings = Settings()
