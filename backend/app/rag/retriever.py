"""知识 Retriever。

职责：只负责根据 query 检索相关知识 chunk，并生成可注入 Prompt 的 context 文本。
不负责 LLM 调用、不直接访问数据库。
"""

from dataclasses import dataclass, field

from app.core.config import settings
from app.rag.embeddings import EmbeddingProvider
from app.rag.vectorstore import VectorStore


@dataclass
class RetrievedChunk:
    """一条检索到的知识片段。"""

    #: 唯一 ID
    id: str
    #: chunk 文本
    content: str
    #: 元数据（source、chunk_seq 等）
    metadata: dict = field(default_factory=dict)
    #: 与 query 的距离（越小越相似）
    distance: float | None = None


class Retriever:
    """基于向量相似度的知识检索器。

    链路::

        query
          ↓
        EmbeddingProvider.embed_texts([query])   # 生成 query 向量
          ↓
        VectorStore.query(top_k)                 # 相似度检索
          ↓
        list[RetrievedChunk]

    Args:
        embedding_provider: 负责向量化的 Embedding Provider。
        vector_store: ChromaDB 持久化向量存储。
        top_k: 返回的最相关片段数量（默认 ``settings.RAG_TOP_K``，4）。
    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        top_k: int | None = None,
    ) -> None:
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.top_k = top_k or settings.RAG_TOP_K

    async def retrieve(
        self, query: str, top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """根据 query 检索相关知识片段。

        Args:
            query: 用户查询文本。
            top_k: 可选覆盖默认返回数量。

        Returns:
            list[RetrievedChunk]: 检索到的知识片段（可能为空列表）。
                空列表表示当前知识库为空或未配置，不会伪造结果。
        """
        k = top_k or self.top_k
        if k <= 0:
            return []

        query = (query or "").strip()
        if not query:
            return []

        # 1. 生成 query 向量
        vectors = await self.embedding_provider.embed_texts([query])
        if not vectors:
            return []
        query_vector = vectors[0]

        # 2. 向量检索
        results = self.vector_store.query(
            query_embedding=query_vector,
            top_k=k,
        )

        # 3. 映射为领域对象
        return [
            RetrievedChunk(
                id=item["id"],
                content=item["document"],
                metadata=item.get("metadata") or {},
                distance=item.get("distance"),
            )
            for item in results
            if item.get("document")
        ]

    def format_context(self, chunks: list[RetrievedChunk]) -> str:
        """把检索到的 chunk 格式化为可注入 System Prompt 的 context 文本。

        Format::

            [来源: travel_beijing.txt | 片段 1]
            <chunk 内容>

            [来源: travel_beijing.txt | 片段 2]
            <chunk 内容>

        Args:
            chunks: 检索到的知识片段。

        Returns:
            str: 格式化后的 context 文本。空列表时返回空字符串。
        """
        if not chunks:
            return ""

        parts: list[str] = []
        for chunk in chunks:
            source = chunk.metadata.get("source") or chunk.metadata.get(
                "path", "unknown"
            )
            seq = chunk.metadata.get("chunk_seq", "")
            header = f"[来源: {source}"
            if seq:
                header += f" | 片段 {seq}"
            header += "]"
            parts.append(f"{header}\n{chunk.content}")

        return "\n\n".join(parts)
