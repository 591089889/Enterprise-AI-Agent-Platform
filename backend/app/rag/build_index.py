"""知识库索引构建脚本（离线开发工具，**不在运行时链路中**）。

用法（在 backend/ 目录下）::

    uv run python -m app.rag.build_index

或强制重建（清空后重新写入）::

    uv run python -m app.rag.build_index --reset

Stage 20 变更说明
-----------------
本脚本面向「本地 ``KNOWLEDGE_DIR/*.txt`` → 默认 collection」这一**离线自检链路**：

- 平台正式 RAG 知识由管理员经 **Knowledge Base → Document → Index** 管理
  （写入 ``{VECTOR_COLLECTION_NAME}__kb_{kb_id}`` 集合）；
- 平台默认对话（``agent_id`` 未指定）**不执行 RAG**，不会读取这里构建的
  默认 collection；
- 项目自 Stage 20 起不再预置任何 ``knowledge/*.txt`` 示例语料，本脚本保留
  用于离线验证 RAG 全链路（Loader → Splitter → Embedding → VectorStore）；
- 目录不存在 / 为空时脚本直接退出，不会创建或清空任何 collection。

职责
----
把 ``knowledge/*.txt`` 通过完整 RAG 链路写入 ChromaDB：

    Knowledge Files
      ↓ Loader
    Documents
      ↓ Splitter
    Chunks
      ↓ Embedding
    Vectors
      ↓ VectorStore
    ChromaDB (持久化)

可重复运行：默认使用 upsert，不会清空已存在数据。
仅当 ``--reset`` 时先删除 Collection 再重建。

异常处理
--------
- 知识目录不存在 / 目录为空 / 无有效 .txt：打印提示并正常退出（exit 0）。
- Embedding 配置缺失（如 dashscope 未配 API Key）：打印错误并异常退出。
"""

import argparse
import asyncio
import sys

from app.core.config import settings
from app.rag.embeddings import get_embedding_provider
from app.rag.loader import KnowledgeLoader
from app.rag.splitter import TextSplitter
from app.rag.vectorstore import VectorStore


def _build_chunk_ids(source: str, seq: int) -> str:
    """生成稳定唯一 chunk ID：``{source}#{seq}``。"""
    return f"{source}#{seq}"


async def build_index(reset: bool = False) -> int:
    """执行知识库索引构建。

    Args:
        reset: 为 True 时先清空 Collection 再重建。

    Returns:
        0 表示成功；1 表示知识库为空或配置缺失（非致命）。
    """
    print(f"[build_index] KNOWLEDGE_DIR={settings.KNOWLEDGE_DIR}")
    print(f"[build_index] VECTOR_STORE_DIR={settings.VECTOR_STORE_DIR}")
    print(f"[build_index] COLLECTION={settings.VECTOR_COLLECTION_NAME}")
    print(f"[build_index] EMBEDDING_PROVIDER={settings.EMBEDDING_PROVIDER}")

    # 1. 加载文档
    loader = KnowledgeLoader()
    documents = loader.load()
    if not documents:
        print(
            "[build_index] 未找到知识文档。"
            f"请确认 {settings.KNOWLEDGE_DIR}/*.txt 存在且非空。"
        )
        return 1

    print(f"[build_index] 加载文档数: {len(documents)}")
    for doc in documents:
        print(f"  - {doc.source} ({len(doc.content)} 字符)")

    # 2. 切分
    splitter = TextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
    )
    chunks = splitter.split(documents)
    if not chunks:
        print("[build_index] 文档切分结果为空。")
        return 1

    print(f"[build_index] 切分 chunk 数: {len(chunks)}")

    # 3. 向量化
    embedding_provider = get_embedding_provider()
    texts = [chunk.content for chunk in chunks]
    print(f"[build_index] 生成 embeddings（{len(texts)} 条）...")
    embeddings = await embedding_provider.embed_texts(texts)
    if not embeddings or len(embeddings) != len(texts):
        print("[build_index] embedding 结果为空或长度不一致，构建失败。")
        return 1
    print(f"[build_index] embedding 维度: {len(embeddings[0])}")

    # 4. 写入 VectorStore
    vector_store = VectorStore()
    if reset:
        print("[build_index] --reset: 清空已有 Collection ...")
        vector_store.reset_collection()

    ids = [
        _build_chunk_ids(chunk.source, chunk.metadata.get("chunk_seq", i + 1))
        for i, chunk in enumerate(chunks)
    ]
    documents_text = [chunk.content for chunk in chunks]
    metadatas = [
        {
            "source": chunk.source,
            "chunk_seq": chunk.metadata.get("chunk_seq", 0),
            "chunk_size": chunk.metadata.get("chunk_size", len(chunk.content)),
        }
        for chunk in chunks
    ]

    vector_store.add_chunks(
        ids=ids,
        embeddings=embeddings,
        documents=documents_text,
        metadatas=metadatas,
    )

    count = vector_store.count()
    print(f"[build_index] 完成！Collection 当前 chunk 数: {count}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 RAG 知识库索引")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先清空已有 Collection 再重建",
    )
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(build_index(reset=args.reset))
    except Exception as exc:  # noqa: BLE001
        print(f"[build_index] 构建失败: {exc}", file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
