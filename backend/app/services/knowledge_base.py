"""知识库 Service 层（Stage 16）。

职责
----
1. **Knowledge Base CRUD**（平台级资源，管理员权限由 Router 的 require_admin 保证）；
2. **Document CRUD**（含 KB ↔ Document 关系校验，跨库访问一律 404）；
3. **文件上传导入**（Stage 22）：把上传的文件解析为纯文本后创建文档
   （``.txt`` / ``.md`` / ``.pdf`` / ``.docx``，解析由
   :func:`app.rag.loader.load_file` 负责）；
4. **索引编排**：复用现有 RAG 模块，把数据库中的原始文档写入该知识库专属的
   Chroma collection；
5. **建库 + 首份文档**（Stage 23）：一个方法内完成「创建知识库 + 导入首份文档」，
   供 ``POST /knowledge-bases`` 的 multipart 形态调用（业务规则 100% 复用 1 / 3）。

上传约定（Stage 22）
--------------------
- **不保存原始文件**：上传流 → ``tempfile`` 临时目录 → Loader 解析 → 目录删除。
  项目内不存在 ``uploads/`` 之类的持久目录，``documents`` 表也不增加
  ``file_path`` / ``storage_key`` / ``oss_url`` 等字段；
- 上传得到的文档与手工创建的文档**完全等价**：只保存正文、``index_status=pending``，
  仍由管理员显式触发索引（**不自动索引**：现有索引实现是「整库重建」，
  批量上传时自动索引会造成 N 次全量重建）；
- 文件类型 / 大小 / 编码问题属于**客户端错误**（400 / 413），不伪装成 502。

约束
----
- 不依赖任何 Web 框架类型（不出现请求 / 响应 / 依赖注入 / 框架异常类）；
- 不执行 commit / rollback（由 ``app.database.session.get_db`` 统一负责）；
- **不修改 RAG 核心模块**：只使用 ``KnowledgeDocument``（dataclass）、
  ``TextSplitter``、``get_embedding_provider``、``VectorStore`` 的既有接口。

向量隔离
--------
每个知识库使用**独立的 Chroma collection**：

    collection_name = f"{settings.VECTOR_COLLECTION_NAME}__kb_{knowledge_base_id}"

- 物理隔离：KB A 的向量不可能出现在 KB B 的检索结果中；
- 默认 collection（``settings.VECTOR_COLLECTION_NAME``，即不带 ``__kb_`` 后缀的那个）
  自 Stage 20 起**不再被任何运行时链路使用**：平台默认对话（``agent_id`` 未指定）
  不执行 RAG，正式知识全部由本 Service 管理的 KB collection 提供 ——
  本 Service 也**不触碰**默认 collection。

重建策略（简单同步，无队列 / 无 Worker）
----------------------------------------
``VectorStore`` 提供两个生命周期操作（Stage 21 起语义分明）：

    reset_collection()   删除 + 重建   → 重新建立索引（索引 / 删除文档时使用）
    drop_collection()    只删除不重建  → 知识库被删除时使用（collection 真正消失）

两者都会**同时清理该 Collection 旧的 HNSW 物理目录**（Stage 21 修复）：
ChromaDB 1.x 的 ``delete_collection()`` 只删元数据、不删物理目录，
长期反复「建索引 → 删知识库」会在 ``.chroma`` 里残留无主 segment 目录。
物理清理完全封装在 ``VectorStore`` 内部（含 Windows 文件锁的延迟清理策略），
本 Service 不接触任何文件系统路径。

``VectorStore`` 仍**没有按 id/metadata 删除**的接口，因此索引与删除文档时采用
「以知识库为粒度重建」：

    reset_collection() → 用 DB 中该 KB 的全部文档重新切分 → 向量化 → add_chunks

好处：不会残留已删除 / 已改短文档的旧 chunk，且**完全不需要修改 RAG 模块**。
代价：重建粒度为知识库（文档数量小时可接受）；若后续需要按文档精确删除，
再为 ``VectorStore`` 增加一个极薄的 delete 方法即可（属后续阶段决定）。
"""

import logging
import tempfile
from pathlib import Path
from typing import BinaryIO

from app.core.config import settings
from app.core.exceptions import AppException, ResourceNotFoundException
from app.models.document import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_PENDING,
    Document,
)
from app.models.knowledge_base import KnowledgeBase
from app.rag.embeddings import get_embedding_provider
from app.rag.exceptions import (
    EmbeddingNotConfiguredException,
    FileLoadException,
    RAGException,
    VectorStoreException,
)
from app.rag.loader import (
    SUPPORTED_UPLOAD_EXTENSIONS,
    KnowledgeDocument,
    load_file,
    supported_extensions_text,
)
from app.rag.splitter import TextSplitter
from app.rag.vectorstore import VectorStore
from app.repositories.document import DocumentRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
)

logger = logging.getLogger(__name__)

#: 面向用户的索引失败文案（不透传上游原始错误 / 内部路径 / Key）
EMBEDDING_FAILED_MESSAGE = (
    "向量化配置缺失或不可用，请检查后端 EMBEDDING_* 配置后重试"
)
VECTOR_STORE_FAILED_MESSAGE = "向量库操作失败，请稍后重试"

#: 文档名称长度上限（与 ``documents.name`` 列宽保持一致）
MAX_DOCUMENT_NAME_LENGTH = 200

#: 文件名中不允许出现的字符（``/`` ``\`` 已在取基础名时处理）
_FORBIDDEN_NAME_CHARS = '<>:"|?*'

#: 上传文件名全为空 / 只剩符号时的兜底名称
_FALLBACK_FILE_NAME = "uploaded_file"


def knowledge_base_collection_name(knowledge_base_id: int) -> str:
    """知识库专属的 Chroma collection 名称（**全项目唯一约定**）。

    - Stage 16（索引写入）与 Stage 17（Agent 对话检索）都使用本函数，
      保证「写入的集合」与「检索的集合」永远一致；
    - ``settings.VECTOR_COLLECTION_NAME`` 在这里仅作为**前缀**使用；
      不带 ``__kb_`` 后缀的默认 collection 自 Stage 20 起不再被运行时使用
      （平台默认对话不执行 RAG）。
    """
    return f"{settings.VECTOR_COLLECTION_NAME}__kb_{knowledge_base_id}"


def _sanitize_upload_filename(filename: str) -> str:
    """把客户端传来的文件名收敛为可安全使用的「基础名」（Stage 22）。

    客户端文件名不可信（可能带目录、控制字符、超长），处理规则：

    1. 只取最后一段路径（兼容浏览器可能附带的 ``C:\\fakepath\\`` 前缀），
       因此 ``..\\..\\x.pdf`` → ``x.pdf``，天然阻断目录穿越；
    2. 去掉控制字符与 ``< > : " | ? *`` 等保留字符；
    3. 去掉首尾空白与结尾的点（Windows 会静默丢弃结尾点）；
    4. 空名 / 只剩符号时回退为 ``uploaded_file``；
    5. 超长按 ``documents.name`` 列宽截断，且**保留扩展名**
       （扩展名决定 Loader 用哪个解析器）。
    """
    raw = (filename or "").replace("\\", "/")
    basename = raw.split("/")[-1]
    cleaned = "".join(
        char
        for char in basename
        if char.isprintable() and char not in _FORBIDDEN_NAME_CHARS
    ).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned.strip("."):
        return _FALLBACK_FILE_NAME

    if len(cleaned) > MAX_DOCUMENT_NAME_LENGTH:
        suffix = Path(cleaned).suffix
        keep = MAX_DOCUMENT_NAME_LENGTH - len(suffix)
        cleaned = f"{cleaned[:keep]}{suffix}"
    return cleaned


def _copy_stream_to_temp(
    stream: BinaryIO, target: Path, max_bytes: int
) -> int:
    """把上传流写入临时文件，**边写边校验**大小上限。

    Args:
        stream: 上传流（``BinaryIO``，由 Router 传入，本层不做任何解析）。
        target: 临时文件路径（位于 ``tempfile.TemporaryDirectory()`` 内）。
        max_bytes: 允许的最大字节数。

    Returns:
        实际写入的字节数。

    Raises:
        AppException: 超过 ``max_bytes``（HTTP 413，立即中止，不再读后续数据）。
    """
    chunk_size = 1024 * 1024
    written = 0
    with target.open("wb") as file_object:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise AppException(
                    "文件过大：单个文件不得超过 "
                    f"{settings.MAX_UPLOAD_FILE_SIZE_MB}MB",
                    status_code=413,
                )
            file_object.write(chunk)
    return written


class KnowledgeBaseService:
    """知识库业务逻辑服务。

    Args:
        knowledge_base_repository: 知识库数据仓库。
        document_repository: 文档数据仓库。
    """

    def __init__(
        self,
        knowledge_base_repository: KnowledgeBaseRepository,
        document_repository: DocumentRepository,
    ) -> None:
        self.knowledge_base_repository = knowledge_base_repository
        self.document_repository = document_repository

    # ------------------------------------------------------------------
    # Knowledge Base CRUD
    # ------------------------------------------------------------------
    async def create_knowledge_base(
        self, data: KnowledgeBaseCreate
    ) -> KnowledgeBase:
        """创建知识库（只写元数据，不涉及向量库）。"""
        knowledge_base = KnowledgeBase(
            name=data.name,
            description=data.description,
        )
        return await self.knowledge_base_repository.create(knowledge_base)

    async def create_knowledge_base_with_upload(
        self,
        data: KnowledgeBaseCreate,
        filename: str,
        stream: BinaryIO,
    ) -> tuple[KnowledgeBase, Document]:
        """创建知识库，并在同一个请求内导入它的首份文档（Stage 23）。

        等价于依次执行两个既有操作::

            POST /knowledge-bases                        # 建知识库
            POST /knowledge-bases/{id}/documents/upload   # 导入首份文档

        合并为一个方法只是省掉一次前端往返，**业务规则完全复用现有实现**：

        - 知识库：只写元数据，不涉及向量库（:meth:`create_knowledge_base`）；
        - 文档：正文来自文件解析、``index_status=pending``、**不自动索引**、
          **不保存原始文件**（:meth:`create_document_from_upload`）。

        事务性（失败绝不留孤儿知识库）
        ----------------------------
        两个步骤都只 ``flush()``、不 ``commit()``：文件类型不支持 / 文件为空 /
        解析失败 / 超过大小上限时 ``AppException`` 向上抛出，由
        ``app.database.session.get_db`` 统一 ``rollback()`` ——
        整个请求（知识库 + 文档）一起回滚，不存在「库建好了、文档失败」的中间态。

        Args:
            data: 知识库元数据（name / description）。
            filename: 客户端提供的文件名（不可信，由上传链路收敛为安全基础名）。
            stream: 二进制上传流（本层读取，Router 不读取文件内容）。

        Returns:
            tuple[KnowledgeBase, Document]: 新建的知识库及其首份文档（均未 commit）。

        Raises:
            AppException: 文件类型不支持 / 文件为空 / 未提取到文本（400），
                或文件超过 ``settings.MAX_UPLOAD_FILE_SIZE_MB``（413）；
                抛出时知识库同样不会被提交。
        """
        knowledge_base = await self.create_knowledge_base(data)
        document = await self.create_document_from_upload(
            knowledge_base.id,
            filename=filename,
            stream=stream,
        )
        return knowledge_base, document

    async def get_knowledge_base(self, knowledge_base_id: int) -> KnowledgeBase:
        """按 ID 获取知识库。

        Raises:
            ResourceNotFoundException: 知识库不存在（HTTP 404）。
        """
        knowledge_base = await self.knowledge_base_repository.get_by_id(
            knowledge_base_id
        )
        if knowledge_base is None:
            raise ResourceNotFoundException(
                f"知识库 ID={knowledge_base_id} 不存在"
            )
        return knowledge_base

    async def list_knowledge_bases(self) -> list[KnowledgeBase]:
        """列出全部知识库（``created_at ASC, id ASC``）。"""
        return await self.knowledge_base_repository.list_all()

    async def update_knowledge_base(
        self, knowledge_base_id: int, data: KnowledgeBaseUpdate
    ) -> KnowledgeBase:
        """部分更新知识库（只更新显式传入的非 None 字段）。"""
        knowledge_base = await self.get_knowledge_base(knowledge_base_id)
        update_data = data.model_dump(exclude_unset=True, exclude_none=True)
        return await self.knowledge_base_repository.update(
            knowledge_base, **update_data
        )

    async def delete_knowledge_base(self, knowledge_base_id: int) -> None:
        """删除知识库。

        - 其下 ``documents`` 由数据库 ``ON DELETE CASCADE`` 一并删除；
        - 该知识库专属的 Chroma collection 做**尽力而为**清理：失败只记日志，
          不让整个删除失败（知识库已在数据库层面消失，其 collection 不会再被
          任何检索使用；collection 名称含自增 id，不会被复用的新知识库撞上）。
          Stage 21 起使用 ``VectorStore.drop_collection()``：collection 被**真正删除**
          （不再残留 count=0 的空集合）。
        """
        knowledge_base = await self.get_knowledge_base(knowledge_base_id)
        await self.knowledge_base_repository.delete(knowledge_base)
        self._drop_collection(knowledge_base_id)

    # ------------------------------------------------------------------
    # Document CRUD
    # ------------------------------------------------------------------
    async def create_document(
        self, knowledge_base_id: int, data: DocumentCreate
    ) -> Document:
        """在指定知识库下创建文档。

        只保存**原始正文**（不做切分 / 向量化）；索引状态为 ``pending``，
        需要管理员显式触发 ``index``。

        Raises:
            ResourceNotFoundException: 知识库不存在（404）。
            AppException: 正文为空（400）。
        """
        await self.get_knowledge_base(knowledge_base_id)

        if not data.content.strip():
            raise AppException("文档正文不能为空", status_code=400)

        document = Document(
            knowledge_base_id=knowledge_base_id,
            name=data.name,
            content=data.content,
            index_status=INDEX_STATUS_PENDING,
        )
        return await self.document_repository.create(document)

    async def create_document_from_upload(
        self,
        knowledge_base_id: int,
        filename: str,
        stream: BinaryIO,
    ) -> Document:
        """上传文件 → 解析为纯文本 → 创建文档（Stage 22）。

        与 :meth:`create_document` 的**结果完全等价**（``index_status=pending``、
        只保存正文），区别仅在于正文来自文件解析而非直接输入。

        文件生命周期（**不保存原始文件**）::

            上传流 → tempfile 临时目录 → Loader 解析 → 目录整体删除

        临时目录由 ``tempfile.TemporaryDirectory()`` 管理：无论成功、失败还是抛异常，
        退出 ``with`` 时整目录（含文件）都会被删除；项目内不创建 ``uploads/`` 之类的
        持久目录，``documents`` 表也不新增 ``file_path`` / ``storage_key`` 字段。
        写入时流式累加并校验大小上限（不把大文件整体读进内存）。

        Args:
            knowledge_base_id: 目标知识库 ID。
            filename: 客户端提供的文件名（不可信，会被收敛为安全基础名）。
            stream: 二进制上传流（本层读取，Router 不读取文件内容）。

        Returns:
            Document: 新建的文档（未 commit，事务由 ``get_db`` 统一负责）。

        Raises:
            ResourceNotFoundException: 知识库不存在（HTTP 404）。
            AppException: 文件类型不支持 / 文件为空 / 未提取到文本（400），
                或文件超过 ``settings.MAX_UPLOAD_FILE_SIZE_MB``（413）。
        """
        await self.get_knowledge_base(knowledge_base_id)

        document_name = _sanitize_upload_filename(filename)
        extension = Path(document_name).suffix.lower()
        if extension not in SUPPORTED_UPLOAD_EXTENSIONS:
            raise AppException(
                f"不支持的文件类型，仅支持 {supported_extensions_text()}",
                status_code=400,
            )

        max_bytes = settings.MAX_UPLOAD_FILE_SIZE_MB * 1024 * 1024
        with tempfile.TemporaryDirectory(prefix="kb_upload_") as temp_dir:
            temp_path = Path(temp_dir) / document_name
            written = _copy_stream_to_temp(stream, temp_path, max_bytes)
            if written == 0:
                raise AppException("上传的文件为空", status_code=400)
            try:
                loaded = load_file(temp_path)
            except FileLoadException as exc:
                # 文件内容本身的问题属于客户端错误（400）；不透传底层异常细节
                raise AppException(exc.message, status_code=400) from exc

        logger.info(
            "知识库 ID=%s 通过上传创建文档：name=%s size=%s bytes（原始文件已删除）",
            knowledge_base_id,
            document_name,
            written,
        )

        document = Document(
            knowledge_base_id=knowledge_base_id,
            name=document_name,
            content=loaded.content,
            index_status=INDEX_STATUS_PENDING,
        )
        return await self.document_repository.create(document)

    async def get_document(
        self, knowledge_base_id: int, document_id: int
    ) -> Document:
        """获取知识库下的文档。

        **资源关系校验**：查询同时限定 ``document_id`` 与 ``knowledge_base_id``，
        因此「用 KB A 的路径访问属于 KB B 的文档」一律 404，
        不会返回其它知识库的数据，也不泄露其是否存在。
        """
        await self.get_knowledge_base(knowledge_base_id)
        document = await self.document_repository.get_by_id_and_knowledge_base(
            document_id, knowledge_base_id
        )
        if document is None:
            raise ResourceNotFoundException(f"文档 ID={document_id} 不存在")
        return document

    async def list_documents(self, knowledge_base_id: int) -> list[Document]:
        """列出知识库下的全部文档（``created_at ASC, id ASC``）。"""
        await self.get_knowledge_base(knowledge_base_id)
        return await self.document_repository.list_by_knowledge_base(
            knowledge_base_id
        )

    async def update_document(
        self,
        knowledge_base_id: int,
        document_id: int,
        data: DocumentUpdate,
    ) -> Document:
        """部分更新文档。

        正文被修改时把 ``index_status`` 重置为 ``pending``：旧向量与新正文已不一致，
        必须由管理员显式重新索引（本阶段不做「自动重建」这种隐藏的重操作）。
        """
        document = await self.get_document(knowledge_base_id, document_id)

        update_data = data.model_dump(exclude_unset=True, exclude_none=True)
        if "content" in update_data:
            if not str(update_data["content"]).strip():
                raise AppException("文档正文不能为空", status_code=400)
            update_data["index_status"] = INDEX_STATUS_PENDING

        return await self.document_repository.update(document, **update_data)

    async def delete_document(
        self, knowledge_base_id: int, document_id: int
    ) -> None:
        """删除文档，并**同步重建**该知识库的向量索引。

        必须同步清理向量：否则已删除文档的内容仍可能被检索到。
        若重建失败 → 异常向上抛出 → 整个删除随事务回滚，
        不会出现「文档已删、向量还在」的不一致状态。
        """
        document = await self.get_document(knowledge_base_id, document_id)
        knowledge_base = await self.get_knowledge_base(knowledge_base_id)

        await self.document_repository.delete(document)
        await self._rebuild_index(knowledge_base)

    # ------------------------------------------------------------------
    # 索引编排
    # ------------------------------------------------------------------
    async def index_document(
        self, knowledge_base_id: int, document_id: int
    ) -> tuple[Document, str, str]:
        """为指定文档（重新）建立索引。

        内部实现是**重建该知识库的 collection**（见模块 docstring 的「重建策略」），
        因此调用成功后该知识库下所有文档都处于最新索引状态。

        Returns:
            ``(目标文档, 状态, 失败说明)``；状态为 ``indexed`` 或 ``failed``。

        为什么失败不抛异常：项目的唯一事务边界是 ``get_db``（**成功响应才 commit**），
        若先写 ``failed`` 再抛异常，状态会被 rollback 丢掉。因此把「失败」作为
        明确的**状态**返回并落库（``status="failed"`` + 用户可读 ``message``），
        绝不谎报 ``indexed``。客户端错误（KB / 文档不存在）仍然抛 404。
        """
        document = await self.get_document(knowledge_base_id, document_id)
        knowledge_base = await self.get_knowledge_base(knowledge_base_id)

        async def mark_failed(message: str) -> tuple[Document, str, str]:
            """整库标记 failed，并返回目标文档的最新状态。"""
            await self._mark_all_documents(
                knowledge_base_id, INDEX_STATUS_FAILED
            )
            target = await self._set_index_status(
                knowledge_base_id, document_id, INDEX_STATUS_FAILED
            )
            return target, INDEX_STATUS_FAILED, message

        try:
            await self._rebuild_index(knowledge_base)
        except EmbeddingNotConfiguredException as exc:
            logger.warning("知识库索引失败（embedding 未配置）: %s", exc)
            return await mark_failed(EMBEDDING_FAILED_MESSAGE)
        except RAGException as exc:
            logger.warning("知识库索引失败（向量库）: %s", exc)
            return await mark_failed(VECTOR_STORE_FAILED_MESSAGE)
        except Exception:  # noqa: BLE001 - 未预期异常：记录完整堆栈，对外只给固定文案
            logger.exception("知识库索引出现未预期异常")
            return await mark_failed(VECTOR_STORE_FAILED_MESSAGE)

        # 成功：该 KB 下所有文档都已随重建完成索引
        await self._mark_all_documents(knowledge_base_id, INDEX_STATUS_INDEXED)
        target = await self._set_index_status(
            knowledge_base_id, document_id, INDEX_STATUS_INDEXED
        )
        return target, INDEX_STATUS_INDEXED, ""

    async def _rebuild_index(self, knowledge_base: KnowledgeBase) -> int:
        """重建知识库索引（reset + 全量重新写入），返回写入的 chunk 数。

        只使用 RAG 模块的既有接口：
        ``reset_collection`` → ``TextSplitter`` → ``embed_texts`` → ``add_chunks``。
        """
        documents = await self.document_repository.list_by_knowledge_base(
            knowledge_base.id
        )
        vector_store = self._build_vector_store(knowledge_base.id)

        # 1. 清空旧向量（含已删除 / 已改短文档留下的残留 chunk）
        vector_store.reset_collection()

        # 2. DB 文档 → RAG 的 KnowledgeDocument（不经过文件系统）
        rag_documents = [
            KnowledgeDocument(
                source=document.name,
                content=document.content,
                metadata={
                    "knowledge_base_id": knowledge_base.id,
                    "document_id": document.id,
                    "source": document.name,
                },
            )
            for document in documents
            if document.content.strip()
        ]
        if not rag_documents:
            return 0

        # 3. 切分
        splitter = TextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
        )
        chunks = splitter.split(rag_documents)
        if not chunks:
            return 0

        # 4. 向量化（配置缺失会抛 EmbeddingNotConfiguredException）
        embedding_provider = get_embedding_provider()
        texts = [chunk.content for chunk in chunks]
        embeddings = await embedding_provider.embed_texts(texts)
        if not embeddings or len(embeddings) != len(texts):
            raise VectorStoreException("embedding 结果为空或数量不一致")

        # 5. 写入（ID 稳定可复现：kb{kb}:doc{doc}:{seq}）
        ids = [
            "kb{kb}:doc{doc}:{seq}".format(
                kb=chunk.metadata.get("knowledge_base_id", knowledge_base.id),
                doc=chunk.metadata.get("document_id", 0),
                seq=chunk.metadata.get("chunk_seq", i + 1),
            )
            for i, chunk in enumerate(chunks)
        ]
        metadatas = [
            {
                "knowledge_base_id": chunk.metadata.get(
                    "knowledge_base_id", knowledge_base.id
                ),
                "document_id": chunk.metadata.get("document_id", 0),
                "source": chunk.metadata.get("source", chunk.source),
                "chunk_seq": chunk.metadata.get("chunk_seq", 0),
                "chunk_size": chunk.metadata.get(
                    "chunk_size", len(chunk.content)
                ),
            }
            for chunk in chunks
        ]

        vector_store.add_chunks(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )
        return vector_store.count()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _collection_name(self, knowledge_base_id: int) -> str:
        """该知识库专属的 Chroma collection 名称（知识库之间物理隔离）。

        具体命名规则见模块级函数 :func:`knowledge_base_collection_name`
        （Stage 17 的 Agent 检索复用同一约定）。
        """
        return knowledge_base_collection_name(knowledge_base_id)

    def _build_vector_store(self, knowledge_base_id: int) -> VectorStore:
        """构建指向该知识库 collection 的 VectorStore。

        只覆盖 ``collection_name``，持久化目录仍沿用
        ``settings.VECTOR_STORE_DIR``（默认 collection 不受影响）。
        """
        return VectorStore(
            collection_name=self._collection_name(knowledge_base_id)
        )

    def _drop_collection(self, knowledge_base_id: int) -> None:
        """删除知识库的 collection（**真正删除，不重建**；失败只记日志）。

        Stage 21 变更：改用 ``VectorStore.drop_collection()``。
        此前复用 ``reset_collection()``（删除 + 重建）会在知识库删除后立即留下
        一个**空 collection**（count=0、无数据），在 Chroma 中不断累积无意义集合。

        Stage 21 修复：``drop_collection()`` 同时清理该 collection 的 HNSW
        物理目录。Windows 上若文件仍被索引进程 mmap 占用，清理会被延迟，
        由 ``scripts/cleanup_orphan_chroma_storage.py``（服务停止后执行）完成；
        此时残留目录已无任何元数据引用，不影响任何存活的 collection。
        """
        try:
            self._build_vector_store(knowledge_base_id).drop_collection()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "清理知识库 ID=%s 的向量 collection 失败（已忽略）: %s",
                knowledge_base_id,
                exc,
            )

    async def _mark_all_documents(
        self, knowledge_base_id: int, status: str
    ) -> None:
        """把该知识库下所有文档的索引状态统一设置为 ``status``。

        重建是「整库」行为，因此状态也必须整库统一，
        否则界面会显示与向量库不一致的状态。
        """
        documents = await self.document_repository.list_by_knowledge_base(
            knowledge_base_id
        )
        for document in documents:
            await self.document_repository.update(
                document, index_status=status
            )

    async def _set_index_status(
        self, knowledge_base_id: int, document_id: int, status: str
    ) -> Document:
        """设置单个文档的索引状态并返回最新对象。"""
        document = await self.get_document(knowledge_base_id, document_id)
        return await self.document_repository.update(
            document, index_status=status
        )



