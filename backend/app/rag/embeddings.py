"""Embedding Provider。

职责：只负责初始化 Embedding 模型与生成向量。
不负责加载文档、切分或存储。

支持三种 Provider：
    - hash      本地确定性散列嵌入（开发/链路验证，无外部依赖）
    - ollama    本地 Ollama Embedding（需 Ollama 服务，OpenAI /api/embeddings）
    - dashscope DashScope Text Embedding（需 EMBEDDING_API_KEY）

约束
----
- 当用户显式配置需要外部服务的 Provider（dashscope / ollama）但配置缺失时，
  抛出 :class:`EmbeddingNotConfiguredException`，不允许伪造成功。
- hash provider 仅用于本地链路验证，其向量不具备真实语义，不注入真实 RAG context。
"""

import asyncio
import hashlib
import json
from abc import ABC, abstractmethod

import urllib.error
import urllib.request

from app.core.config import settings
from app.rag.exceptions import EmbeddingNotConfiguredException

#: Hash 嵌入固定向量维度
HASH_EMBEDDING_DIM = 128


class EmbeddingProvider(ABC):
    """Embedding 统一抽象接口。"""

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把文本列表编码为向量列表。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度。"""


class HashEmbeddingProvider(EmbeddingProvider):
    """确定性字符散列嵌入（仅用于开发/链路验证）。

    - 不发起任何网络请求。
    - 对任意文本生成固定维度（128）的确定性向量。
    - 适用于验证 Loader → Splitter → Embedding → VectorStore → Retriever 全链路。
    """

    @property
    def dimension(self) -> int:
        return HASH_EMBEDDING_DIM

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for i, char in enumerate(text):
            digest = hashlib.sha256(
                f"{char}:{i}".encode("utf-8")
            ).digest()
            # 用一个字节确定向量桶，另一个字节确定符号/权重
            bucket = digest[0] % self.dimension
            sign = 1.0 if (digest[1] & 1) else -1.0
            vec[bucket] += sign * (digest[2] / 255.0)
        # L2 归一化，保证余弦相似度可比较
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Ollama 本地 Embedding Provider。

    调用 Ollama 的 OpenAI Compatible ``/v1/embeddings`` 端点。
    需要本地 Ollama 服务运行且已拉取对应 embedding 模型。
    """

    MAX_BATCH_SIZE = 16

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = (
            base_url or settings.EMBEDDING_BASE_URL or "http://localhost:11434"
        ).rstrip("/")
        self.model = model or settings.EMBEDDING_MODEL or "nomic-embed-text"

    @property
    def dimension(self) -> int:
        return 0  # 未知，由模型决定

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[i: i + self.MAX_BATCH_SIZE]
            results.extend(await self._embed_batch(batch))
        return results

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        def _sync_call() -> list[list[float]]:
            url = f"{self.base_url}/v1/embeddings"
            payload = {"model": self.model, "input": texts}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            try:
                return [item["embedding"] for item in data["data"]]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(
                    f"Unexpected Ollama embedding response: {data}")

        return await asyncio.to_thread(_sync_call)


class DashScopeEmbeddingProvider(EmbeddingProvider):
    """DashScope（阿里云百炼）Text Embedding Provider。

    调用 DashScope OpenAI Compatible ``/embeddings`` 端点。
    默认模型 ``text-embedding-v4``。
    """

    DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL = "text-embedding-v4"
    MAX_BATCH_SIZE = 16

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.model = model or settings.EMBEDDING_MODEL or self.DEFAULT_MODEL
        self.base_url = (
            base_url
            or settings.EMBEDDING_BASE_URL
            or self.DASHSCOPE_BASE_URL
        ).rstrip("/")

        if not self.api_key:
            raise EmbeddingNotConfiguredException(
                "EMBEDDING_PROVIDER=dashscope 需要配置 EMBEDDING_API_KEY。"
                "请修改 backend/.env 或使用 EMBEDDING_PROVIDER=hash 仅做链路验证。"
            )

    @property
    def dimension(self) -> int:
        return 0  # 由模型决定

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[i: i + self.MAX_BATCH_SIZE]
            results.extend(await self._embed_batch(batch))
        return results

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        def _sync_call() -> list[list[float]]:
            url = f"{self.base_url}/embeddings"
            payload = {"model": self.model, "input": texts}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            try:
                return [item["embedding"] for item in data["data"]]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(
                    f"Unexpected DashScope embedding response: {data}"
                )

        return await asyncio.to_thread(_sync_call)


def get_embedding_provider() -> EmbeddingProvider:
    """按 ``settings.EMBEDDING_PROVIDER`` 工厂创建 Embedding Provider。

    支持:
        - ``hash``      → :class:`HashEmbeddingProvider`（默认，本地链路验证）
        - ``ollama``    → :class:`OllamaEmbeddingProvider`
        - ``dashscope`` → :class:`DashScopeEmbeddingProvider`
    """
    provider_name = (
        settings.EMBEDDING_PROVIDER or "hash"
    ).strip().lower()

    if provider_name == "ollama":
        return OllamaEmbeddingProvider()
    if provider_name == "dashscope":
        return DashScopeEmbeddingProvider()
    return HashEmbeddingProvider()
