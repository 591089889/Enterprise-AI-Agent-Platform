"""Vector Store（ChromaDB 持久化）。

职责：只负责向量存储的初始化、写入、查询与持久化。
不负责加载文档、切分或生成向量。

Collection 生命周期与物理存储（Stage 21 修复）
--------------------------------------------
ChromaDB 1.x（本项目使用 1.5.9）的 ``client.delete_collection()`` **只删除
SysDB 元数据**（``chroma.sqlite3`` 里的 ``collections`` / ``segments`` 行），
**不会删除 HNSW 物理目录** —— 即 ``<persist_dir>/<VECTOR segment id>/``
（``data_level0.bin`` / ``header.bin`` / ``length.bin`` / ``link_lists.bin``）。
因此长期反复「建索引 → 删知识库」会在 ``.chroma`` 里残留大量无主目录。

本模块把物理清理**封装在 VectorStore 内部**，调用方（Service / Router）不接触文件系统：

    reset_collection()   删除 + 重建（重建索引）→ 同时清理旧物理目录
    drop_collection()    只删除、不重建（知识库删除）→ 同时清理物理目录

两者都返回物理清理报告 ``{"removed": [...], "deferred": [...]}``（目录名列表），
便于调用方（如维护脚本）如实记录「真正删掉了什么、什么被延迟」。

映射来源（不做任何猜测）
------------------------
ChromaDB **没有**通过公开 API 暴露 segment id（``collection.id`` ≠ HNSW 目录名），
所以这里只读 ChromaDB 自己维护的 ``chroma.sqlite3``：``collections.name`` →
``collections.id`` → ``segments``（``scope='VECTOR'``）的 ``id`` 即物理目录名。
读不到元数据时一律**放弃**物理清理（逻辑删除仍由官方 API 完成），绝不按名字猜。

维护脚本为什么也不能用「官方枚举 API」
--------------------------------------
「谁加载过索引，谁就持有 mmap」。因此**执行物理清理的进程本身**也必须避免加载
HNSW：维护脚本枚举 collection 时读取 :func:`collection_names` /
:func:`embedding_count`（只读 ``chroma.sqlite3``），而不是
``client.list_collections()`` + ``collection.count()`` —— 后者会把每个 collection
的 HNSW 载入当前进程，于是它自己就变成了「占用者」，一个残留都删不掉。
同理，「删除 collection（会加载索引）」与「删除物理目录」必须分属两个进程：
维护脚本把前者交给子进程（子进程退出即释放句柄），自己只做后者。

跨平台约束
----------
HNSW 索引文件被**加载它的进程**内存映射（mmap）：在 Windows 上，只要该进程存活，
文件就无法被删除（``WinError 32``），同一进程的 ``clear_system_cache()`` /
``client.close()`` / ``del client`` 都不会释放（已实测），ChromaDB 也未提供卸载
API。因此物理删除是**尽力而为**：

- Linux/macOS：unlink 不受 mmap 影响，删除立即成功；
- Windows：索引进程内通常失败，删除被「延迟」到下一个不再持有 mmap 的进程 ——
  即由 ``scripts/cleanup_orphan_chroma_storage.py``（服务停止后执行）完成。

被延迟的目录一定是**已无元数据引用**的无主残留（可安全清理），
不会影响任何仍然存活的 collection。
"""

import logging
import shutil
import sqlite3
import uuid
from pathlib import Path

import chromadb
from chromadb.api.models.Collection import Collection

from app.core.config import settings
from app.rag.exceptions import VectorStoreException

logger = logging.getLogger(__name__)

#: ChromaDB 元数据库文件名（位于持久化目录内）
CHROMA_SQLITE_FILENAME = "chroma.sqlite3"

#: HNSW VECTOR segment 的类型前缀（``urn:chroma:segment/vector/hnsw-*``）
_HNSW_SEGMENT_TYPE_PREFIX = "urn:chroma:segment/vector/hnsw"


# ----------------------------------------------------------------------
# ChromaDB 元数据（只读）→ 物理目录映射
# ----------------------------------------------------------------------
def _open_chroma_metadata(persist_dir: str) -> sqlite3.Connection | None:
    """只读打开 ChromaDB 自身的元数据库 ``chroma.sqlite3``。

    返回 ``None`` 表示元数据不可读（文件不存在 / 无法打开），
    调用方必须据此放弃物理清理（不做任何猜测）。
    """
    db_path = Path(persist_dir) / CHROMA_SQLITE_FILENAME
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0
        )
    except sqlite3.Error:
        return None


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    """执行一条只读查询；失败（表结构不符 / 锁冲突）返回空列表。"""
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []


def hnsw_segment_ids(persist_dir: str, collection_name: str) -> list[str]:
    """返回该 Collection 对应的 HNSW VECTOR segment id（= 物理目录名）。

    数据来源是 ChromaDB 自己写入的元数据表（权威映射，非猜测）：

        collections.name → collections.id → segments.id
        （segments.scope = 'VECTOR' 且 type 为 hnsw-local-persisted）

    元数据不可读或该 collection 不存在时返回空列表。
    """
    conn = _open_chroma_metadata(persist_dir)
    if conn is None:
        return []
    try:
        rows = _fetch(
            conn,
            "SELECT s.id FROM segments AS s "
            "JOIN collections AS c ON c.id = s.collection "
            "WHERE c.name = ? AND s.scope = 'VECTOR' AND s.type LIKE ?",
            (collection_name, f"{_HNSW_SEGMENT_TYPE_PREFIX}%"),
        )
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def collection_names(persist_dir: str) -> list[str]:
    """返回 ChromaDB 元数据中的全部 collection 名称（只读，**不加载任何索引**）。

    与 ``chromadb.ClientAPI.list_collections()`` 的区别：本函数只读
    ``chroma.sqlite3`` 的 ``collections`` 表，不会实例化 collection / segment，
    因此**不会把 HNSW 文件 mmap 到当前进程**。维护脚本
    （``scripts/cleanup_orphan_chroma_storage.py``）在清理前必须用本函数枚举集合：
    一旦当前进程加载过某个 HNSW，该目录在进程退出前任何进程都删不掉（Windows）。

    元数据不可读时返回空列表（宁可不清理，也不误判）。
    """
    conn = _open_chroma_metadata(persist_dir)
    if conn is None:
        return []
    try:
        rows = _fetch(conn, "SELECT name FROM collections")
        return sorted(str(row[0]) for row in rows)
    finally:
        conn.close()


def embedding_count(persist_dir: str, collection_name: str) -> int:
    """返回该 Collection 的向量行数（只读元数据，**不加载** HNSW 索引）。

    数据来源：``embeddings.segment_id`` → ``segments.collection`` → ``collections.name``。
    collection 不存在或元数据不可读时返回 ``0``。
    """
    conn = _open_chroma_metadata(persist_dir)
    if conn is None:
        return 0
    try:
        rows = _fetch(
            conn,
            "SELECT COUNT(*) FROM embeddings AS e "
            "JOIN segments AS s ON s.id = e.segment_id "
            "JOIN collections AS c ON c.id = s.collection "
            "WHERE c.name = ?",
            (collection_name,),
        )
        return int(rows[0][0]) if rows else 0
    finally:
        conn.close()


def referenced_storage_ids(persist_dir: str) -> set[str]:
    """返回 ChromaDB 元数据中仍被引用的全部 id（collections.id ∪ segments.id）。

    这些 id **不允许**被物理清理：任何一个都可能对应存活 Collection 的 HNSW 目录。
    """
    conn = _open_chroma_metadata(persist_dir)
    if conn is None:
        return set()
    try:
        ids = {str(row[0]) for row in _fetch(conn, "SELECT id FROM collections")}
        ids |= {str(row[0]) for row in _fetch(conn, "SELECT id FROM segments")}
        return {item.lower() for item in ids}
    finally:
        conn.close()


def orphan_storage_dirs(persist_dir: str) -> list[Path]:
    """列出持久化目录中**已无任何元数据引用**的物理残留目录。

    判定条件（全部满足才算残留，任何一条不满足都跳过）：

    1. 位于 ``persist_dir`` 下的一级目录；
    2. 目录名可解析为 UUID（ChromaDB 的 segment id 形态）；
    3. 目录名不在 ``collections`` / ``segments`` 的任何 id 中。

    元数据不可读时返回空列表（宁可不清理，也不误删）。
    """
    base = Path(persist_dir)
    if not base.is_dir():
        return []
    conn = _open_chroma_metadata(persist_dir)
    if conn is None:
        return []
    conn.close()

    referenced = referenced_storage_ids(persist_dir)
    orphans: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            # 非 UUID 目录（例如 ``_lifecycle_probe``）不是 ChromaDB segment 目录
            continue
        if child.name.lower() in referenced:
            continue
        orphans.append(child)
    return orphans


class VectorStore:
    """ChromaDB 持久化向量存储。

    Args:
        persist_dir: ChromaDB 持久化目录（相对 backend/ 或绝对路径）。
            默认 ``settings.VECTOR_STORE_DIR``（".chroma"）。
        collection_name: Collection 名称。
            默认 ``settings.VECTOR_COLLECTION_NAME``（"knowledge_chunks"）。
    """

    def __init__(
        self,
        persist_dir: str | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.persist_dir = persist_dir or settings.VECTOR_STORE_DIR
        self.collection_name = collection_name or settings.VECTOR_COLLECTION_NAME

        self._client: chromadb.ClientAPI | None = None
        self._collection: Collection | None = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _ensure_client(self) -> chromadb.ClientAPI:
        """惰性初始化 ChromaDB PersistentClient。

        - 目录不存在时自动创建。
        - 失败时抛出 :class:`VectorStoreException`。
        """
        if self._client is not None:
            return self._client

        try:
            persist_path = str(Path(self.persist_dir))
            self._client = chromadb.PersistentClient(path=persist_path)
        except Exception as exc:  # noqa: BLE001 - 统一包装为领域异常
            raise VectorStoreException(
                f"ChromaDB 初始化失败: {exc}"
            ) from exc
        return self._client

    def _ensure_collection(self) -> Collection:
        """惰性获取（或创建）Collection。

        注意：不使用 ChromaDB 内置 embedding_function，
        由外部 EmbeddingProvider 生成向量后显式传入。
        """
        if self._collection is not None:
            return self._collection

        client = self._ensure_client()
        try:
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreException(
                f"ChromaDB Collection 获取/创建失败: {exc}"
            ) from exc
        return self._collection

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add_chunks(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict] | None = None,
    ) -> None:
        """批量写入 chunk。

        Args:
            ids: 唯一 ID 列表（必须与 embeddings/documents 等长）。
            embeddings: 向量列表。
            documents: chunk 文本列表。
            metadatas: 元数据列表（可选）。
        """
        if len(ids) != len(embeddings) or len(ids) != len(documents):
            raise VectorStoreException(
                "ids / embeddings / documents 长度不一致"
            )

        if not ids:
            return

        collection = self._ensure_collection()
        try:
            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreException(f"ChromaDB 写入失败: {exc}") from exc

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        where: dict | None = None,
    ) -> list[dict]:
        """根据 query 向量检索最相似的 chunk。

        Args:
            query_embedding: query 的向量。
            top_k: 返回的候选数量。
            where: 可选元数据过滤条件（ChromaDB where 语法）。

        Returns:
            list[dict]: 每个 dict 包含 ``id`` / ``document`` / ``metadata`` /
                ``distance`` 字段。
        """
        collection = self._ensure_collection()

        try:
            result = collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreException(f"ChromaDB 查询失败: {exc}") from exc

        # ChromaDB 返回嵌套结构；映射为简单 dict 列表
        items: list[dict] = []
        ids = result.get("ids", [[]])[0] or []
        documents = result.get("documents", [[]])[0] or []
        metadatas = result.get("metadatas", [[]])[0] or []
        distances = result.get("distances", [[]])[0] or []

        for i, chunk_id in enumerate(ids):
            items.append(
                {
                    "id": chunk_id,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else None,
                }
            )
        return items

    # ------------------------------------------------------------------
    # 统计 / 清空
    # ------------------------------------------------------------------
    def count(self) -> int:
        """返回当前 Collection 中的 chunk 数量。"""
        collection = self._ensure_collection()
        try:
            return collection.count()
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreException(
                f"ChromaDB count 失败: {exc}"
            ) from exc

    def reset_collection(self) -> dict[str, list[str]]:
        """删除并重建 Collection（用于重复构建知识库时清理旧数据）。

        与 :meth:`drop_collection` 的语义区别（两个生命周期操作必须分明）::

            reset_collection()   删除 + 立即重建 → 重新建立索引（rebuild）
            drop_collection()    只删除，不重建  → 知识库已删除，collection 应真正不存在

        Stage 21 起：删除同时清理该 Collection 旧的 HNSW 物理目录，
        避免反复重建索引在持久化目录里堆积无主 segment 目录。

        Returns:
            dict: 旧物理目录清理报告 ``{"removed": [...], "deferred": [...]}``
                （目录名列表，语义同 :meth:`drop_collection`）。
        """
        client = self._ensure_client()
        # 官方 API 删除前先解析物理目录（删除后映射即消失，无法再查）
        stale_dirs = hnsw_segment_ids(self.persist_dir, self.collection_name)
        try:
            client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001
            # 如果 collection 不存在，忽略
            pass
        # 强制下次重新获取 collection
        self._collection = None
        report = self._remove_storage_dirs(stale_dirs)
        self._ensure_collection()
        return report

    def drop_collection(self) -> dict[str, list[str]]:
        """删除 Collection 且**不重建**（用于知识库被删除，Stage 21）。

        与 :meth:`reset_collection` 的语义区别（两个生命周期操作必须分明）::

            reset_collection()   删除 + 立即重建 → 重新建立索引（rebuild）
            drop_collection()    只删除，不重建  → 知识库已删除，collection 应真正不存在

        行为约定：

        - collection 本来就不存在时视为**成功**（幂等），不抛异常；
        - 其它失败（ChromaDB 异常）抛出 :class:`VectorStoreException`，
          由调用方（``KnowledgeBaseService._drop_collection``）决定是否容忍；
        - **不调用** ``_ensure_collection()``，因此删除后不会被重新创建出来；
        - 删除成功后清理该 Collection 的 HNSW 物理目录（尽力而为，
          详见模块文档「跨平台约束」：Windows 上可能被延迟到服务停止后的维护进程）。

        Returns:
            dict: 物理目录清理报告 ``{"removed": [...], "deferred": [...]}``
                （目录名列表）。``deferred`` 表示因文件被当前进程占用（Windows
                mmap）未能删除，需在不再持有 mmap 的进程里重试，例如
                ``scripts/cleanup_orphan_chroma_storage.py``。
        """
        client = self._ensure_client()
        # 官方 API 删除前先解析物理目录（删除后映射即消失，无法再查）
        stale_dirs = hnsw_segment_ids(self.persist_dir, self.collection_name)
        try:
            client.delete_collection(self.collection_name)
        except Exception as exc:  # noqa: BLE001
            if self._collection_exists(client):
                # 仍能列出该 collection → 是真实的删除失败，明确报错
                raise VectorStoreException(
                    f"ChromaDB 删除 Collection 失败: {exc}"
                ) from exc
            # 不存在 → 幂等成功
        finally:
            # 缓存句柄失效（即使删除失败也不应继续复用旧句柄）
            self._collection = None
        return self._remove_storage_dirs(stale_dirs)

    def orphan_storage_dirs(self) -> list[Path]:
        """列出本持久化目录内**已无元数据引用**的物理残留目录（只读，不删除）。"""
        return orphan_storage_dirs(self.persist_dir)

    def purge_orphan_storage(self) -> dict[str, list[str]]:
        """清理持久化目录内**已无元数据引用**的物理残留目录。

        只清理可被证明是残留的目录（判定条件见
        :func:`orphan_storage_dirs`）：仍然存活的 Collection / segment
        被元数据引用着，**绝不**会被删除。默认 collection
        （``knowledge_chunks``）只要存在就受此保护。

        Returns:
            dict: ``{"removed": [...], "deferred": [...]}`` —— 目录名列表。
                ``deferred`` 表示因文件被当前进程占用（Windows mmap）而未能删除，
                需在不再持有 mmap 的进程里重试（``scripts/cleanup_orphan_chroma_storage.py``）。
        """
        names = [path.name for path in self.orphan_storage_dirs()]
        return self._remove_storage_dirs(names)

    def _remove_storage_dirs(self, dir_names: list[str]) -> dict[str, list[str]]:
        """尽最大努力删除指定的物理目录（``segment id == 目录名``）。

        删除前做三重防御（任一不满足即跳过，报错只记录不抛出）：

        1. 目录名必须是 UUID；
        2. 目录必须与 ``persist_dir`` 同级（即只能是 ``persist_dir/<name>``）；
        3. 该名字必须**已不被** ChromaDB 元数据引用（collections / segments）。
        """
        report: dict[str, list[str]] = {"removed": [], "deferred": []}
        if not dir_names:
            return report

        referenced = referenced_storage_ids(self.persist_dir)
        base = Path(self.persist_dir)
        for name in dir_names:
            try:
                uuid.UUID(name)
            except ValueError:
                continue
            target = base / name
            if target.parent != base or not target.is_dir():
                continue
            if name.lower() in referenced:
                # 仍被 Collection / segment 引用 → 保护，绝不删除
                continue
            try:
                shutil.rmtree(target)
            except OSError as exc:
                report["deferred"].append(name)
                logger.warning(
                    "ChromaDB 物理目录暂无法删除（进程占用，稍后由维护脚本清理）: "
                    "%s (%s)",
                    target,
                    exc,
                )
            else:
                report["removed"].append(name)
                logger.info("已清理 ChromaDB 物理目录: %s", target)
        return report

    def _collection_exists(self, client: chromadb.ClientAPI) -> bool:
        """当前 collection 是否仍存在于 ChromaDB。

        仅用于 :meth:`drop_collection` 区分「本来就不存在」与「删除失败」。
        """
        try:
            return any(
                collection.name == self.collection_name
                for collection in client.list_collections()
            )
        except Exception:  # noqa: BLE001
            # 无法枚举时保守认为「存在」，让调用方看到真实错误
            return True
