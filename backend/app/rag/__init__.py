"""RAG Knowledge Base 包。

本包实现最小可用的本地知识库 RAG 链路：

    Knowledge Files
        ↓
    Loader        读取 backend/knowledge/*.txt
        ↓
    Splitter      文本切分（chunk_size / chunk_overlap）
        ↓
    Embeddings    向量化（hash / ollama / dashscope）
        ↓
    VectorStore   ChromaDB 持久化
        ↓
    Retriever     query → top_k relevant chunks

职责边界
--------
- Loader      ：只负责读取本地知识文档。
- Splitter    ：只负责把文本切成 chunk。
- Embeddings  ：只负责初始化/调用 Embedding 模型。
- VectorStore ：只负责向量存储的增删查与持久化。
- Retriever   ：只负责根据 query 检索相关 chunk。

本包不依赖 FastAPI / Database / AgentRuntime；
AgentRuntime 不感知本包（保持 Runtime 与向量检索解耦）。
"""

from app.rag.embeddings import (
    EmbeddingProvider,
    get_embedding_provider,
)
from app.rag.exceptions import (
    EmbeddingNotConfiguredException,
    KnowledgeEmptyException,
    RAGException,
    VectorStoreException,
)
from app.rag.loader import KnowledgeLoader
from app.rag.retriever import Retriever
from app.rag.splitter import TextSplitter
from app.rag.vectorstore import VectorStore

__all__ = [
    "EmbeddingNotConfiguredException",
    "EmbeddingProvider",
    "KnowledgeEmptyException",
    "KnowledgeLoader",
    "RAGException",
    "Retriever",
    "TextSplitter",
    "VectorStore",
    "VectorStoreException",
    "get_embedding_provider",
]
