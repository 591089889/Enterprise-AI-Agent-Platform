"""RAG 领域异常定义。

本模块定义 RAG 相关异常，保持轻量：
- 不依赖 FastAPI / HTTPException。
- 由 AgentService 捕获并转换为业务错误，或由 Router 层映射为 HTTP 状态码。
"""


class RAGException(Exception):
    """RAG 模块基础异常。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class KnowledgeEmptyException(RAGException):
    """知识库为空。

    当 ``knowledge/*.txt`` 不存在、目录为空，或没有任何可切分文档时抛出。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EmbeddingNotConfiguredException(RAGException):
    """Embedding 配置缺失。

    当用户显式选择需要外部服务的 Embedding Provider
    （如 dashscope / ollama），但对应配置缺失时抛出。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class VectorStoreException(RAGException):
    """Vector Store 操作失败。

    当 ChromaDB 初始化、写入或查询失败时抛出。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FileLoadException(RAGException):
    """单文件加载 / 解析失败（Stage 22）。

    当上传的文件类型不被支持、内容损坏 / 加密、或提取不到文本时抛出。

    与 :class:`VectorStoreException` 的区别：这是**客户端错误**（用户上传的文件
    本身有问题），因此 Service 会把它映射为 HTTP 400，而不是 502。
    ``message`` 必须是面向用户的固定文案，不透传底层库的原始异常
    （可能包含临时文件路径等内部信息）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
