"""文本切分器。

职责：只负责把文档文本切成可检索的 chunk。
不负责加载文档、向量化或存储。

使用基于字符数 + 重叠窗口的切分策略（不依赖 langchain）：
- chunk_size：每个 chunk 的目标字符数（默认 500）。
- chunk_overlap：相邻 chunk 之间的重叠字符数（默认 50）。
"""

from dataclasses import dataclass, field

from app.rag.loader import KnowledgeDocument


@dataclass
class TextChunk:
    """一个文本切分片段。"""

    #: 所属文档来源（文件名）
    source: str
    #: chunk 文本
    content: str
    #: 元数据（包含文档元数据 + chunk 序号）
    metadata: dict = field(default_factory=dict)


class TextSplitter:
    """基于定长窗口 + 重叠的文本切分器。"""

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be non-negative")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, documents: list[KnowledgeDocument]) -> list[TextChunk]:
        """把多个文档切成 chunk 列表。

        Args:
            documents: 加载到的知识文档列表。

        Returns:
            list[TextChunk]: 切分后的 chunk 列表（按文档顺序）。
                当文档列表为空时返回空列表。
        """
        chunks: list[TextChunk] = []
        for doc in documents:
            text = doc.content.strip()
            if not text:
                continue

            start = 0
            seq = 0
            while start < len(text):
                end = start + self.chunk_size
                piece = text[start:end].strip()

                if piece:
                    seq += 1
                    chunks.append(
                        TextChunk(
                            source=doc.source,
                            content=piece,
                            metadata={
                                **doc.metadata,
                                "chunk_seq": seq,
                                "chunk_size": len(piece),
                            },
                        )
                    )

                # 如果已到文本末尾则结束
                if end >= len(text):
                    break

                # 前进到下一个窗口起点，扣除重叠部分
                start = end - self.chunk_overlap

        return chunks
