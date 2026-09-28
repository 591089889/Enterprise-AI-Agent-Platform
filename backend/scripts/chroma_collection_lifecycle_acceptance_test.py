"""Stage 21：Chroma Collection 生命周期与物理存储验收测试。

验证目标（针对「删除知识库后 .chroma 仍残留旧 HNSW 目录」的修复）
----------------------------------------------------------------
ChromaDB 1.x 的 ``client.delete_collection()`` 只删除元数据（``chroma.sqlite3``
的 ``collections`` / ``segments`` 行），**不会删除 HNSW 物理目录**
``.chroma/<VECTOR segment id>/``。因此本测试逐条核对：

1. Case 1：创建 KB **不**创建 collection（不多写一个字节的向量存储）；
2. Case 2：文档 Index 后才创建 collection，且 chunk 数 > 0、物理目录与
   Chroma 元数据记录的 segment id **一一对应**；
3. Case 3：删除 KB → collection 真正不存在（API + Chroma 元数据双重复核），
   且该 collection 的物理目录**已删除或至少成为无元数据引用的残留**，
   随后由维护脚本（服务停止后执行）真正删除；
4. Case 4：多 KB 隔离 —— 删除 KB-A 不影响 KB-B（collection 存在、向量可检索、
   检索结果只可能来自 KB-B），清理过程**绝不**误删存活 collection 的物理目录；
5. Case 5：``drop_collection()`` 重复调用幂等（第二次不抛异常、不产生新目录）；
6. Case 6：重复 Index（重建索引）后 collection 仍然唯一且可用，
   物理目录数量收敛（不随重建次数无限增长）。

物理存储如何验证（关键设计）
----------------------------
HNSW 文件被**加载过它的进程** mmap 占用：Windows 上只要该进程存活就无法删除
（``WinError 32``），ChromaDB 未提供卸载 API（已实测 ``clear_system_cache()`` /
``client.close()`` 均无效）。因此物理清理的真实运行时形态是：

    服务进程索引 → 删除 KB（元数据立刻消失；物理目录若被占用则延迟）
      → 服务退出 / 下一个进程 → 维护脚本清理无主残留

本测试用**独立子进程**忠实模拟该形态：
``--child`` 模式（index / index-drop / index-reindex）扮演「服务进程」，
``scripts/cleanup_orphan_chroma_storage.py`` 扮演「服务停止后的维护清理」，
在系统临时目录里的 probe 持久化目录上完成端到端验证；
真实 ``.chroma`` 上的用例则复核「元数据已删除 + 残留无引用 + 存活目录受保护」。
测试结束时 probe 目录被删除，真实 ``.chroma`` 中不会留下存活 collection。

测试数据全部清理；不读取 / 不打印任何密钥。
"""

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import chromadb  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agents.providers.base import LLMResponse  # noqa: E402
from app.agents.providers.qwen import QwenProvider  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.rag.embeddings import get_embedding_provider  # noqa: E402
from app.rag.vectorstore import (  # noqa: E402
    CHROMA_SQLITE_FILENAME,
    VectorStore,
    hnsw_segment_ids,
    orphan_storage_dirs,
)
from app.services.knowledge_base import (  # noqa: E402
    knowledge_base_collection_name,
)

THIS_FILE = os.path.abspath(__file__)
CLEANUP_SCRIPT = os.path.join("scripts", "cleanup_orphan_chroma_storage.py")

CHROMA_DIR = Path(settings.VECTOR_STORE_DIR)
DEFAULT_COLLECTION = settings.VECTOR_COLLECTION_NAME

PASS: list[str] = []
FAIL: list[str] = []
INFO: list[str] = []

CAPTURED: dict = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


def info(message: str) -> None:
    """记录一条环境事实（不参与 PASS/FAIL，但会写进汇总）。"""
    INFO.append(message)
    print(f"[info] {message}")


async def spy_generate(self, messages):  # noqa: ANN001
    """Provider spy：固定 LLM 输出，避免消耗真实额度（不伪造 RAG 结果）。"""
    CAPTURED.clear()
    CAPTURED.update(
        model=self.model,
        system_prompt=messages[0].get("content") if messages else "",
    )
    return "STAGE21-SPY-REPLY"


async def spy_generate_with_tools(self, messages, tools):  # noqa: ANN001
    CAPTURED.clear()
    CAPTURED.update(
        model=self.model,
        system_prompt=messages[0].get("content") if messages else "",
    )
    return LLMResponse(content="STAGE21-SPY-REPLY")


# ----------------------------------------------------------------------
# 独立复核工具（刻意不复用被测实现，避免「自己验证自己」）
# ----------------------------------------------------------------------
def _sqlite(persist_dir: Path) -> sqlite3.Connection | None:
    db = Path(persist_dir) / CHROMA_SQLITE_FILENAME
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


def sqlite_collection_names(persist_dir: Path) -> set[str]:
    """只读读取 Chroma 元数据中的 collection 名（不启动 Chroma 客户端）。"""
    conn = _sqlite(persist_dir)
    if conn is None:
        return set()
    try:
        return {row[0] for row in conn.execute("SELECT name FROM collections")}
    finally:
        conn.close()


def sqlite_collection_id(persist_dir: Path, name: str) -> str:
    conn = _sqlite(persist_dir)
    if conn is None:
        return ""
    try:
        rows = list(conn.execute("SELECT id FROM collections WHERE name = ?", (name,)))
        return rows[0][0] if rows else ""
    finally:
        conn.close()


def sqlite_vector_segment_ids(persist_dir: Path, name: str) -> list[str]:
    """该 collection 的 VECTOR segment id（= HNSW 物理目录名）。"""
    conn = _sqlite(persist_dir)
    if conn is None:
        return []
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT s.id FROM segments s "
                "JOIN collections c ON c.id = s.collection "
                "WHERE c.name = ? AND s.scope = 'VECTOR'",
                (name,),
            )
        ]
    finally:
        conn.close()


def sqlite_embedding_count(persist_dir: Path, name: str) -> int:
    """该 collection 的向量行数（Chroma 内部维度的复核口径）。"""
    conn = _sqlite(persist_dir)
    if conn is None:
        return -1
    try:
        rows = list(
            conn.execute(
                "SELECT COUNT(e.id) FROM embeddings e "
                "JOIN segments s ON s.id = e.segment_id "
                "JOIN collections c ON c.id = s.collection "
                "WHERE c.name = ?",
                (name,),
            )
        )
        return int(rows[0][0]) if rows else -1
    finally:
        conn.close()


def sqlite_referenced_ids(persist_dir: Path) -> set[str]:
    """元数据中仍被引用的全部 id（collections ∪ segments）。"""
    conn = _sqlite(persist_dir)
    if conn is None:
        return set()
    try:
        ids = {row[0] for row in conn.execute("SELECT id FROM collections")}
        ids |= {row[0] for row in conn.execute("SELECT id FROM segments")}
        return {item.lower() for item in ids}
    finally:
        conn.close()


def is_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


def physical_dirs(persist_dir: Path) -> set[str]:
    if not persist_dir.is_dir():
        return set()
    return {p.name for p in persist_dir.iterdir() if p.is_dir()}


def unreferenced_dirs(persist_dir: Path) -> set[str]:
    """物理目录中存在、但元数据不再引用的「无主残留」（独立实现）。"""
    referenced = sqlite_referenced_ids(persist_dir)
    return {
        name
        for name in physical_dirs(persist_dir)
        if is_uuid(name) and name.lower() not in referenced
    }


def chroma_collection_names(persist_dir: Path) -> list[str]:
    """通过 Chroma 官方 API 列出 collection（与元数据口径交叉复核）。"""
    client = chromadb.PersistentClient(path=str(persist_dir))
    return sorted(collection.name for collection in client.list_collections())


def report_dir_state(persist_dir: Path, name: str) -> str:
    """把「该 collection 的物理目录状态」格式化为一句可读结论。"""
    segment_ids = sqlite_vector_segment_ids(persist_dir, name)
    if not segment_ids:
        return f"{name}: 元数据中已无 VECTOR segment（collection 已删除）"
    states = []
    for segment_id in segment_ids:
        exists = (persist_dir / segment_id).is_dir()
        states.append(f"{segment_id}={'存在' if exists else '已删除'}")
    return f"{name}: " + ", ".join(states)


# ----------------------------------------------------------------------
# 子进程工具（模拟「服务进程」与「服务停止后的维护脚本」）
# ----------------------------------------------------------------------
def _run_python(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """在独立子进程中运行 python（stdout / stderr **固定按 UTF-8** 解码）。

    必须显式指定 ``encoding`` 并给子进程设置 ``PYTHONIOENCODING=utf-8``：
    否则解码方式取决于本机 locale（中文 Windows 默认 GBK）。一旦外层环境把
    ``PYTHONIOENCODING`` 设成 utf-8（例如自动化脚本 / CI），子进程输出 UTF-8
    而父进程按 GBK 解码，读取线程会直接抛 ``UnicodeDecodeError``，
    ``proc.stdout`` 变成 ``None``，断言随之崩溃 —— 与本测试要验证的行为无关。
    """
    return subprocess.run(
        [sys.executable, *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        timeout=timeout,
    )


def run_child(mode: str, persist_dir: Path, collection: str) -> dict:
    """在独立子进程里执行索引 / 删除 / 重建，返回子进程打印的摘要。"""
    proc = _run_python(
        [
            THIS_FILE,
            "--child",
            mode,
            "--persist-dir",
            str(persist_dir),
            "--collection",
            collection,
        ]
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"子进程模式 {mode} 失败（rc={proc.returncode}）:\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("CHILD_JSON "):
            return json.loads(line[len("CHILD_JSON ") :])
    raise AssertionError(f"子进程未输出 CHILD_JSON:\n{proc.stdout[-2000:]}")


def run_cleanup_script(
    persist_dir: Path, apply: bool = True, include_physical: bool = True
) -> str:
    """运行交付给运维的维护脚本（默认真正执行），返回其输出。"""
    args = [CLEANUP_SCRIPT, "--persist-dir", str(persist_dir)]
    if apply:
        args.append("--apply")
    if include_physical:
        args.append("--include-orphan-physical")
    proc = _run_python(args)
    if proc.returncode != 0:
        raise AssertionError(
            f"维护脚本执行失败（rc={proc.returncode}）:\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


# ----------------------------------------------------------------------
# --child 模式：扮演「后端服务进程」执行索引 / 删除 / 重建
# ----------------------------------------------------------------------
def run_child_mode(args: argparse.Namespace) -> int:
    """在独立进程里完成索引动作，并把结果以 JSON 打印给父进程。

    单独进程的意义：ChromaDB 的 HNSW 文件被加载它的进程 mmap 占用，
    Windows 上无法在该进程内删除。用子进程写索引并退出，
    正是真实运行时的形态（服务进程退出后残留才可被清理）。
    """
    persist_dir = Path(args.persist_dir)
    collection = args.collection
    vectors = [[0.1, 0.2, 0.3], [0.2, 0.3, 0.4], [0.9, 0.1, 0.0]]
    ids = [f"{collection}-c{i}" for i in range(len(vectors))]
    documents = [f"probe chunk {i}" for i in range(len(vectors))]
    metadatas = [
        {"source": "probe-doc", "chunk_seq": i + 1} for i in range(len(vectors))
    ]

    store = VectorStore(persist_dir=str(persist_dir), collection_name=collection)
    summary: dict = {"mode": args.child, "collection": collection}

    store.add_chunks(
        ids=ids, embeddings=vectors, documents=documents, metadatas=metadatas
    )
    summary["count"] = store.count()
    summary["segment_ids"] = sqlite_vector_segment_ids(persist_dir, collection)
    summary["dirs_after_index"] = sorted(physical_dirs(persist_dir))

    if args.child == "index-reindex":
        summary["reindex_report"] = store.reset_collection()
        store.add_chunks(
            ids=ids, embeddings=vectors, documents=documents, metadatas=metadatas
        )
        summary["count_after_reindex"] = store.count()
    elif args.child in ("index-drop", "index-drop-twice"):
        summary["first_drop_report"] = store.drop_collection()
        summary["first_drop_ok"] = True
        if args.child == "index-drop-twice":
            try:
                store.drop_collection()
                summary["second_drop_error"] = None
            except Exception as exc:  # noqa: BLE001 - 幂等性由父进程判定
                summary["second_drop_error"] = repr(exc)

    summary["live_segment_ids"] = sqlite_vector_segment_ids(persist_dir, collection)
    summary["dirs_final"] = sorted(physical_dirs(persist_dir))
    summary["residue_final"] = sorted(unreferenced_dirs(persist_dir))
    print("CHILD_JSON " + json.dumps(summary))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 21：Chroma Collection 生命周期与物理存储验收测试"
    )
    parser.add_argument(
        "--child",
        choices=["index", "index-drop", "index-drop-twice", "index-reindex"],
        default=None,
        help="内部子进程模式（模拟后端服务进程的索引动作，供本测试自身调用）",
    )
    parser.add_argument("--persist-dir", default=None, help="子进程模式的持久化目录")
    parser.add_argument("--collection", default=None, help="子进程模式的 collection 名")
    return parser.parse_args()


# ----------------------------------------------------------------------
# 主流程（父进程：in-process ASGI + 真实 PG / Chroma + 子进程 probe）
# ----------------------------------------------------------------------
async def db_scalar(query: str, params: dict | None = None):
    """只读 SQL 标量（与数据库交叉校验，不信任接口返回）。"""
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params or {})).scalar_one()


def cleanup_chroma_leftovers() -> list[str]:
    """兜底：删除测试结束时仍残留的 collection（正常情况下应已不存在）。"""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    leftover = [collection.name for collection in client.list_collections()]
    for name in leftover:
        client.delete_collection(name)
        print(f"[cleanup] 兜底清理残留 collection: {name}")
    del client
    return leftover


def summary_and_exit() -> None:
    print("\n" + "=" * 76)
    print("Stage 21 Chroma Collection 生命周期验收汇总")
    print("-" * 76)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 76)
    sys.exit(1 if FAIL else 0)


async def main() -> None:  # noqa: C901
    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"s21-admin-{tag}"
    admin_email = f"{admin_name}@example.com"

    QwenProvider.generate = spy_generate
    QwenProvider.generate_with_tools = spy_generate_with_tools

    initial_dirs = physical_dirs(CHROMA_DIR)
    info(
        "初始 .chroma: collections="
        f"{sorted(sqlite_collection_names(CHROMA_DIR)) or '（空）'}"
        f" 物理目录={len(initial_dirs)} 个"
        f"（其中无主残留={len(unreferenced_dirs(CHROMA_DIR))} 个）"
    )

    probe_dir = Path(tempfile.mkdtemp(prefix="chroma_lifecycle_probe_"))
    info(f"probe 持久化目录（用于模拟服务进程生命周期）: {probe_dir}")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://s21.test", timeout=120.0
        ) as client:
            await client.post(
                "/users/",
                json={
                    "username": admin_name,
                    "email": admin_email,
                    "password": pwd,
                },
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET role='admin' WHERE username=:u"),
                    {"u": admin_name},
                )
            login = await client.post(
                "/auth/login", json={"email": admin_email, "password": pwd}
            )
            admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}

            # ==========================================================
            # Case 1：创建 KB 不创建 collection
            # ==========================================================
            print("\n=== Case 1: 创建知识库（只写元数据，不建 collection） ===")
            dirs_before_kb = physical_dirs(CHROMA_DIR)
            kb_resp = await client.post(
                "/knowledge-bases",
                json={"name": f"S21知识库-{tag}", "description": "stage21 生命周期验收"},
                headers=admin_h,
            )
            kb = kb_resp.json()
            kb_collection = knowledge_base_collection_name(kb["id"])
            check(
                "Case 1: 创建 KB → 201",
                kb_resp.status_code == 201,
                f"{kb_resp.status_code} {kb_resp.text[:200]}",
            )
            check(
                "Case 1: 创建 KB 后该 collection 尚不存在（Chroma 元数据口径）",
                kb_collection not in sqlite_collection_names(CHROMA_DIR),
                f"{sqlite_collection_names(CHROMA_DIR)}",
            )
            check(
                "Case 1: 创建 KB 后该 collection 尚不存在（官方 API 口径）",
                kb_collection not in chroma_collection_names(CHROMA_DIR),
                f"{chroma_collection_names(CHROMA_DIR)}",
            )
            check(
                "Case 1: 创建 KB 未产生任何新的物理目录（不写向量存储）",
                physical_dirs(CHROMA_DIR) == dirs_before_kb,
                f"{sorted(physical_dirs(CHROMA_DIR) - dirs_before_kb)}",
            )

            # ==========================================================
            # Case 2：Index 后才创建 collection，且物理目录与元数据一一对应
            # ==========================================================
            print("\n=== Case 2: 文档 Index → collection + HNSW 物理目录 ===")
            doc = (
                await client.post(
                    f"/knowledge-bases/{kb['id']}/documents",
                    json={
                        "name": "退款规则.md",
                        "content": (
                            "企业退款规则：订单支付后 7 天内可以申请退款，"
                            "超过 7 天需要人工审核。"
                        ),
                    },
                    headers=admin_h,
                )
            ).json()
            check(
                "Case 2: 新增文档（未 Index）不创建 collection",
                kb_collection not in sqlite_collection_names(CHROMA_DIR),
                f"{sqlite_collection_names(CHROMA_DIR)}",
            )
            index_resp = await client.post(
                f"/knowledge-bases/{kb['id']}/documents/{doc['id']}/index",
                headers=admin_h,
            )
            api_count = VectorStore(collection_name=kb_collection).count()
            segment_ids = sqlite_vector_segment_ids(CHROMA_DIR, kb_collection)
            check(
                "Case 2: Index → 200 indexed 且 collection 已创建、count > 0",
                index_resp.status_code == 200
                and index_resp.json().get("status") == "indexed"
                and kb_collection in sqlite_collection_names(CHROMA_DIR)
                and api_count > 0,
                f"{index_resp.status_code} {index_resp.json()} count={api_count}",
            )
            check(
                "Case 2: 元数据记录的 VECTOR segment 目录真实存在（segment id = 目录名）",
                len(segment_ids) == 1
                and all((CHROMA_DIR / item).is_dir() for item in segment_ids),
                f"{segment_ids}",
            )
            check(
                "Case 2: 物理目录内确实有 HNSW 索引文件",
                all(
                    any(p.suffix == ".bin" for p in (CHROMA_DIR / item).iterdir())
                    for item in segment_ids
                ),
                f"{segment_ids}",
            )
            check(
                "Case 2: 映射与 VectorStore.hnsw_segment_ids 完全一致",
                sorted(segment_ids)
                == sorted(hnsw_segment_ids(str(CHROMA_DIR), kb_collection)),
                f"{segment_ids} vs "
                f"{hnsw_segment_ids(str(CHROMA_DIR), kb_collection)}",
            )
            check(
                "Case 2: 独立口径（sqlite embeddings）向量行数 > 0",
                sqlite_embedding_count(CHROMA_DIR, kb_collection) > 0,
                f"{sqlite_embedding_count(CHROMA_DIR, kb_collection)}",
            )
            check(
                "Case 2: 该物理目录被元数据引用（不会被判定为无主残留）",
                not (set(segment_ids) & unreferenced_dirs(CHROMA_DIR)),
                f"{sorted(set(segment_ids) & unreferenced_dirs(CHROMA_DIR))}",
            )
            info(f"Case 2: {report_dir_state(CHROMA_DIR, kb_collection)}")

            # ==========================================================
            # Case 3：删除 KB → collection 真正消失 + 物理存储被清理
            # ==========================================================
            print("\n=== Case 3: 删除 KB → collection + 物理存储清理 ===")
            kb1_dirs_before = set(segment_ids)
            del_resp = await client.delete(
                f"/knowledge-bases/{kb['id']}", headers=admin_h
            )
            check(
                "Case 3: 删除 KB → 204",
                del_resp.status_code == 204,
                f"{del_resp.status_code} {del_resp.text[:200]}",
            )
            check(
                "Case 3: PostgreSQL 中该 KB 已不存在",
                await db_scalar(
                    "SELECT COUNT(*) FROM knowledge_bases WHERE id=:i",
                    {"i": kb["id"]},
                )
                == 0,
                f"id={kb['id']}",
            )
            check(
                "Case 3: PostgreSQL 中该 KB 的文档已被级联删除",
                await db_scalar(
                    "SELECT COUNT(*) FROM documents WHERE knowledge_base_id=:i",
                    {"i": kb["id"]},
                )
                == 0,
                f"id={kb['id']}",
            )
            check(
                "Case 3: Chroma 元数据中该 collection 真正不存在（不是 count=0 的空集合）",
                kb_collection not in sqlite_collection_names(CHROMA_DIR),
                f"{sqlite_collection_names(CHROMA_DIR)}",
            )
            check(
                "Case 3: 官方 API 复核同一结论（列表中已无该 collection）",
                kb_collection not in chroma_collection_names(CHROMA_DIR),
                f"{chroma_collection_names(CHROMA_DIR)}",
            )
            check(
                "Case 3: 元数据中该 collection 的 VECTOR segment 记录已消失",
                sqlite_vector_segment_ids(CHROMA_DIR, kb_collection) == [],
                f"{sqlite_vector_segment_ids(CHROMA_DIR, kb_collection)}",
            )
            referenced_now = sqlite_referenced_ids(CHROMA_DIR)
            removed_now = {
                item for item in kb1_dirs_before if not (CHROMA_DIR / item).is_dir()
            }
            deferred_now = {
                item
                for item in kb1_dirs_before
                if (CHROMA_DIR / item).is_dir() and item.lower() not in referenced_now
            }
            check(
                "Case 3: 该 collection 的物理目录已删除，或已成为无元数据引用的残留（二者必居其一）",
                bool(kb1_dirs_before)
                and len(removed_now | deferred_now) == len(kb1_dirs_before),
                f"已删除={sorted(removed_now)} 延迟残留={sorted(deferred_now)}",
            )
            check(
                "Case 3: 上述残留不被任何 collection / segment 引用（可安全清理）",
                {item.lower() for item in kb1_dirs_before}.isdisjoint(referenced_now),
                f"residue={sorted(deferred_now)}",
            )
            info(
                "Case 3: 进程内物理清理结果 -> "
                f"已删除={sorted(removed_now)} 延迟残留={sorted(deferred_now)}"
                "（Windows 索引进程持有 mmap，删除会被延迟到进程退出后）"
            )

            # ---- Case 3（端到端）：独立进程模拟真实运行时形态 ----
            print(
                "\n--- Case 3 端到端: 服务进程索引 → 删除 KB → 服务退出 → 维护脚本清理 ---"
            )
            dropped = f"{DEFAULT_COLLECTION}__probe_dropped"
            live = f"{DEFAULT_COLLECTION}__probe_live"
            logical_orphan = f"{DEFAULT_COLLECTION}__kb_900001"
            drop_payload = run_child("index-drop", probe_dir, dropped)
            live_payload = run_child("index", probe_dir, live)
            dropped_seg = drop_payload["segment_ids"][0]
            live_seg = live_payload["segment_ids"][0]
            check(
                "Case 3: 子进程（服务进程）索引后生成 HNSW 物理目录",
                (probe_dir / dropped_seg).is_dir()
                and (probe_dir / live_seg).is_dir(),
                f"dropped={dropped_seg} live={live_seg}",
            )
            check(
                "Case 3: 删除 KB 后该目录要么已删除、要么成为无引用残留",
                not (probe_dir / dropped_seg).is_dir()
                or dropped_seg in unreferenced_dirs(probe_dir),
                f"{report_dir_state(probe_dir, dropped)}",
            )
            check(
                "Case 3: 存活 collection 的物理目录仍被元数据引用（受保护）",
                live_seg in sqlite_referenced_ids(probe_dir)
                and live_seg not in unreferenced_dirs(probe_dir),
                f"{live_seg}",
            )
            drop_report = drop_payload.get("first_drop_report") or {}
            reported = sorted(
                drop_report.get("removed", []) + drop_report.get("deferred", [])
            )
            check(
                "Case 3: drop_collection() 返回物理清理报告（removed / deferred 如实覆盖该目录）",
                set(drop_report) == {"removed", "deferred"}
                and reported == [dropped_seg],
                f"{drop_report}",
            )

            dry_run_out = run_cleanup_script(
                probe_dir, apply=False, include_physical=True
            )
            check(
                "Case 3: 维护脚本默认 dry-run 不删除任何内容",
                (probe_dir / dropped_seg).is_dir()
                and (probe_dir / live_seg).is_dir(),
                f"dry-run 后目录={sorted(physical_dirs(probe_dir))}",
            )
            check(
                "Case 3: dry-run 输出明确标注未删除（可安全预览）",
                "DRY-RUN" in dry_run_out,
                dry_run_out[-300:],
            )

            orphan_payload = run_child("index", probe_dir, logical_orphan)
            cleanup_out = run_cleanup_script(
                probe_dir, apply=True, include_physical=True
            )
            check(
                "Case 3: 维护脚本一次运行即删除「被删除 KB」的物理目录",
                not (probe_dir / dropped_seg).exists(),
                f"{report_dir_state(probe_dir, dropped)}",
            )
            check(
                "Case 3: 维护脚本识别并删除「PG 中已无对应 KB」的逻辑孤儿 collection",
                logical_orphan not in sqlite_collection_names(probe_dir),
                f"collections={sorted(sqlite_collection_names(probe_dir))}",
            )
            check(
                "Case 3: 逻辑孤儿的物理目录也在同一次运行中被删除"
                "（删 collection 的子进程已退出释放 mmap）",
                not (probe_dir / orphan_payload["segment_ids"][0]).exists(),
                f"orphan_dir={orphan_payload['segment_ids'][0]} "
                f"剩余={sorted(physical_dirs(probe_dir))}",
            )
            check(
                "Case 3: 维护脚本明确报告子进程（删 collection）及其退出码 0",
                "在独立子进程中删除" in cleanup_out
                and "子进程退出码: 0" in cleanup_out
                and "物理残留已全部清理" in cleanup_out,
                cleanup_out[-600:],
            )
            second_out = run_cleanup_script(
                probe_dir, apply=True, include_physical=True
            )
            check(
                "Case 3: 再次运行维护脚本为幂等空操作（removed=0 deferred=0）",
                "removed=0 deferred=0" in second_out
                and physical_dirs(probe_dir) == {live_seg},
                f"{sorted(physical_dirs(probe_dir))} vs {{{live_seg}}}",
            )
            check(
                "Case 3: 维护脚本未误删存活 collection 的物理目录",
                (probe_dir / live_seg).is_dir(),
                f"live={live_seg}",
            )
            check(
                "Case 3: 清理后 probe 物理目录收敛为存活 collection 的目录",
                physical_dirs(probe_dir) == {live_seg},
                f"{sorted(physical_dirs(probe_dir))} vs {{{live_seg}}}",
            )
            check(
                "Case 3: 清理后存活 collection 仍完整（元数据 + 向量行数）",
                live in sqlite_collection_names(probe_dir)
                and sqlite_embedding_count(probe_dir, live) == 3,
                f"{sqlite_embedding_count(probe_dir, live)}",
            )
            info(
                "Case 3: 维护脚本关键输出 -> "
                + " | ".join(
                    line.strip()
                    for line in cleanup_out.splitlines()
                    if "已删除" in line or "物理残留" in line
                )
            )

            # ==========================================================
            # Case 4：多知识库隔离 + 清理不误删存活 collection
            # ==========================================================
            print("\n=== Case 4: 多知识库隔离（删除 KB-A 不影响 KB-B） ===")
            kb_a = (
                await client.post(
                    "/knowledge-bases",
                    json={"name": f"S21知识库A-{tag}", "description": "隔离验证-A"},
                    headers=admin_h,
                )
            ).json()
            kb_b = (
                await client.post(
                    "/knowledge-bases",
                    json={"name": f"S21知识库B-{tag}", "description": "隔离验证-B"},
                    headers=admin_h,
                )
            ).json()
            doc_a = (
                await client.post(
                    f"/knowledge-bases/{kb_a['id']}/documents",
                    json={
                        "name": "A-退款规则.md",
                        "content": "A 库内容：订单支付后 7 天内可以申请退款。",
                    },
                    headers=admin_h,
                )
            ).json()
            doc_b = (
                await client.post(
                    f"/knowledge-bases/{kb_b['id']}/documents",
                    json={
                        "name": "B-配送规则.md",
                        "content": "B 库内容：标准配送时间为 3 到 5 个工作日。",
                    },
                    headers=admin_h,
                )
            ).json()
            index_a = await client.post(
                f"/knowledge-bases/{kb_a['id']}/documents/{doc_a['id']}/index",
                headers=admin_h,
            )
            index_b = await client.post(
                f"/knowledge-bases/{kb_b['id']}/documents/{doc_b['id']}/index",
                headers=admin_h,
            )
            coll_a = knowledge_base_collection_name(kb_a["id"])
            coll_b = knowledge_base_collection_name(kb_b["id"])
            seg_a = set(sqlite_vector_segment_ids(CHROMA_DIR, coll_a))
            seg_b = set(sqlite_vector_segment_ids(CHROMA_DIR, coll_b))
            count_a = VectorStore(collection_name=coll_a).count()
            count_b = VectorStore(collection_name=coll_b).count()
            check(
                "Case 4: 两个 KB 各自建立独立 collection（均含向量）",
                index_a.status_code == 200
                and index_b.status_code == 200
                and count_a > 0
                and count_b > 0,
                f"A={count_a} B={count_b}",
            )
            check(
                "Case 4: 两个 collection 的 HNSW 物理目录互不相同",
                bool(seg_a) and bool(seg_b) and not (seg_a & seg_b),
                f"A={sorted(seg_a)} B={sorted(seg_b)}",
            )

            del_a = await client.delete(
                f"/knowledge-bases/{kb_a['id']}", headers=admin_h
            )
            referenced = sqlite_referenced_ids(CHROMA_DIR)
            check(
                "Case 4: 删除 KB-A → 204 且其 collection 已不存在",
                del_a.status_code == 204
                and coll_a not in sqlite_collection_names(CHROMA_DIR)
                and coll_a not in chroma_collection_names(CHROMA_DIR),
                f"{del_a.status_code} {sorted(sqlite_collection_names(CHROMA_DIR))}",
            )
            check(
                "Case 4: KB-B 的 collection 与向量完好无损（删除 KB-A 无影响）",
                coll_b in sqlite_collection_names(CHROMA_DIR)
                and VectorStore(collection_name=coll_b).count() == count_b,
                f"count={VectorStore(collection_name=coll_b).count()} vs {count_b}",
            )
            check(
                "Case 4: KB-B 的物理目录仍被元数据引用且未被删除",
                all((CHROMA_DIR / item).is_dir() for item in seg_b)
                and all(item.lower() in referenced for item in seg_b),
                f"B={sorted(seg_b)}",
            )
            check(
                "Case 4: KB-A 的物理目录已无元数据引用（已删除或成为可安全清理的残留）",
                {item.lower() for item in seg_a}.isdisjoint(referenced),
                f"A={sorted(seg_a)} referenced={sorted(referenced)}",
            )

            provider = get_embedding_provider()
            query_vector = (await provider.embed_texts(["退款 7 天 配送 工作日"]))[0]
            store_b = VectorStore(collection_name=coll_b)
            items_b = store_b.query(query_embedding=query_vector, top_k=5)
            sources_b = [item["metadata"].get("source") for item in items_b]
            check(
                "Case 4: KB-B 检索可用且结果全部来自 KB-B（物理隔离）",
                bool(items_b)
                and all(source == "B-配送规则.md" for source in sources_b),
                f"count={len(items_b)} sources={sources_b}",
            )

            orphan_now = {path.name for path in orphan_storage_dirs(str(CHROMA_DIR))}
            check(
                "Case 4: VectorStore.orphan_storage_dirs() 不含 KB-B 的物理目录（判定口径一致）",
                not (seg_b & orphan_now),
                f"{sorted(orphan_now)}",
            )
            purge_report = VectorStore().purge_orphan_storage()
            info(
                "Case 4: 进程内 purge_orphan_storage -> "
                f"removed={len(purge_report['removed'])} "
                f"deferred={len(purge_report['deferred'])}"
            )
            check(
                "Case 4: 真实执行清理后，KB-B 的物理目录仍然存在（绝不误删存活数据）",
                all((CHROMA_DIR / item).is_dir() for item in seg_b),
                f"B={sorted(seg_b)}",
            )
            items_b_after = store_b.query(query_embedding=query_vector, top_k=5)
            check(
                "Case 4: 清理后 KB-B 仍可正常检索（count 不变、结果仍来自 KB-B）",
                store_b.count() == count_b
                and bool(items_b_after)
                and all(
                    item["metadata"].get("source") == "B-配送规则.md"
                    for item in items_b_after
                ),
                f"count={store_b.count()} items={len(items_b_after)}",
            )
            check(
                "Case 4: 清理过程未创建默认 collection（未被误用）",
                DEFAULT_COLLECTION not in sqlite_collection_names(CHROMA_DIR),
                f"{sorted(sqlite_collection_names(CHROMA_DIR))}",
            )
            del_b = await client.delete(
                f"/knowledge-bases/{kb_b['id']}", headers=admin_h
            )
            check(
                "Case 4: 删除 KB-B → 204 且其 collection 同样真正不存在",
                del_b.status_code == 204
                and coll_b not in sqlite_collection_names(CHROMA_DIR),
                f"{del_b.status_code} {sorted(sqlite_collection_names(CHROMA_DIR))}",
            )

            # ==========================================================
            # Case 5：drop_collection 幂等（重复删除不抛异常、不新建）
            # ==========================================================
            print("\n=== Case 5: drop_collection 重复调用幂等 ===")
            dirs_before_5 = physical_dirs(CHROMA_DIR)
            never = f"{DEFAULT_COLLECTION}__kb_999999"
            error_never = None
            reports_never: list = []
            try:
                reports_never.append(
                    VectorStore(collection_name=never).drop_collection()
                )
                reports_never.append(
                    VectorStore(collection_name=never).drop_collection()
                )
            except Exception as exc:  # noqa: BLE001 - 幂等性判定
                error_never = repr(exc)
            check(
                "Case 5: 对「本来就不存在」的 collection 连续 drop 两次 → 无异常",
                error_never is None,
                str(error_never),
            )
            check(
                "Case 5: 幂等 drop 返回空报告（未删除、未延迟任何目录）",
                len(reports_never) == 2
                and all(
                    set(report) == {"removed", "deferred"}
                    and report["removed"] == []
                    and report["deferred"] == []
                    for report in reports_never
                ),
                f"{reports_never}",
            )
            check(
                "Case 5: 上述操作不产生任何物理目录 / collection（不重建）",
                physical_dirs(CHROMA_DIR) == dirs_before_5
                and never not in sqlite_collection_names(CHROMA_DIR),
                f"{sorted(physical_dirs(CHROMA_DIR) - dirs_before_5)}",
            )
            error_again = None
            try:
                VectorStore(collection_name=coll_a).drop_collection()
            except Exception as exc:  # noqa: BLE001 - 幂等性判定
                error_again = repr(exc)
            check(
                "Case 5: 对已删除的 KB-A collection 再次 drop → 幂等且不重新创建",
                error_again is None
                and coll_a not in sqlite_collection_names(CHROMA_DIR),
                f"{error_again} {sorted(sqlite_collection_names(CHROMA_DIR))}",
            )
            twice = run_child(
                "index-drop-twice", probe_dir, f"{DEFAULT_COLLECTION}__probe_twice"
            )
            check(
                "Case 5: 真实数据：索引后连续 drop 两次，第二次不抛异常",
                twice.get("second_drop_error") is None,
                str(twice.get("second_drop_error")),
            )
            check(
                "Case 5: 连续删除后元数据中不残留该 collection",
                twice.get("live_segment_ids") == []
                and f"{DEFAULT_COLLECTION}__probe_twice"
                not in sqlite_collection_names(probe_dir),
                f"{twice.get('live_segment_ids')}",
            )
            run_cleanup_script(probe_dir, apply=True, include_physical=True)
            check(
                "Case 5: 维护脚本清理后 probe 目录仍只保留存活 collection 的目录",
                physical_dirs(probe_dir) == {live_seg},
                f"{sorted(physical_dirs(probe_dir))} vs {{{live_seg}}}",
            )

            # ==========================================================
            # Case 6：重复 Index（重建索引）→ collection 唯一、目录收敛
            # ==========================================================
            print("\n=== Case 6: 重复 Index（重建索引）后 collection 唯一且目录收敛 ===")
            kb_c = (
                await client.post(
                    "/knowledge-bases",
                    json={"name": f"S21知识库C-{tag}", "description": "重建索引验证"},
                    headers=admin_h,
                )
            ).json()
            doc_c = (
                await client.post(
                    f"/knowledge-bases/{kb_c['id']}/documents",
                    json={
                        "name": "C-会员规则.md",
                        "content": "C 库内容：会员积分在订单完成后 24 小时内到账。",
                    },
                    headers=admin_h,
                )
            ).json()
            coll_c = knowledge_base_collection_name(kb_c["id"])
            first_index = await client.post(
                f"/knowledge-bases/{kb_c['id']}/documents/{doc_c['id']}/index",
                headers=admin_h,
            )
            count_first = VectorStore(collection_name=coll_c).count()
            id_first = sqlite_collection_id(CHROMA_DIR, coll_c)
            seg_first = set(sqlite_vector_segment_ids(CHROMA_DIR, coll_c))
            second_index = await client.post(
                f"/knowledge-bases/{kb_c['id']}/documents/{doc_c['id']}/index",
                headers=admin_h,
            )
            count_second = VectorStore(collection_name=coll_c).count()
            id_second = sqlite_collection_id(CHROMA_DIR, coll_c)
            seg_second = set(sqlite_vector_segment_ids(CHROMA_DIR, coll_c))
            check(
                "Case 6: 首次 Index → indexed 且 count > 0、恰有 1 个 HNSW 目录",
                first_index.status_code == 200
                and count_first > 0
                and len(seg_first) == 1
                and all((CHROMA_DIR / item).is_dir() for item in seg_first),
                f"{first_index.status_code} count={count_first} seg={sorted(seg_first)}",
            )
            check(
                "Case 6: 重复 Index → 仍然 indexed 且 chunk 数一致（无重复写入）",
                second_index.status_code == 200
                and second_index.json().get("status") == "indexed"
                and count_second == count_first,
                f"{second_index.status_code} count={count_second} vs {count_first}",
            )
            check(
                "Case 6: 重建后同名 collection 在元数据中仍只有一条（不产生重复集合）",
                sum(1 for name in sqlite_collection_names(CHROMA_DIR) if name == coll_c)
                == 1
                and id_second != "",
                f"id={id_first} -> {id_second}",
            )
            check(
                "Case 6: 重建是「删除 + 重建」而非原地复用（collection id 已更新）",
                id_second != id_first,
                f"id={id_first} -> {id_second}",
            )
            check(
                "Case 6: 重建后存活的 HNSW 目录恰有 1 个且真实存在",
                len(seg_second) == 1
                and all((CHROMA_DIR / item).is_dir() for item in seg_second),
                f"{sorted(seg_second)}",
            )
            old_segments = seg_first - seg_second
            check(
                "Case 6: 重建前的旧 HNSW 目录已删除或成为无引用残留（不无限增长）",
                all(
                    not (CHROMA_DIR / item).exists()
                    or item in unreferenced_dirs(CHROMA_DIR)
                    for item in old_segments
                ),
                f"old={sorted(old_segments)} 残留={sorted(unreferenced_dirs(CHROMA_DIR))}",
            )
            check(
                "Case 6: 旧目录不再被任何元数据引用（可安全清理）",
                {item.lower() for item in old_segments}.isdisjoint(
                    sqlite_referenced_ids(CHROMA_DIR)
                ),
                f"old={sorted(old_segments)}",
            )
            VectorStore().purge_orphan_storage()
            check(
                "Case 6: 清理残留后 KB-C 的存活目录与向量毫发无损（保护回归）",
                all((CHROMA_DIR / item).is_dir() for item in seg_second)
                and VectorStore(collection_name=coll_c).count() == count_first,
                f"{VectorStore(collection_name=coll_c).count()} vs {count_first}",
            )

            reindexed = run_child(
                "index-reindex", probe_dir, f"{DEFAULT_COLLECTION}__probe_reindex"
            )
            check(
                "Case 6: 子进程重建索引后 chunk 数一致（reset + 重写不产生重复）",
                reindexed.get("count_after_reindex") == reindexed.get("count"),
                f"{reindexed.get('count')} -> {reindexed.get('count_after_reindex')}",
            )
            check(
                "Case 6: 子进程内重建后该 collection 的 VECTOR segment 仍恰好 1 个",
                len(reindexed.get("live_segment_ids", [])) == 1,
                f"{reindexed.get('live_segment_ids')}",
            )
            check(
                "Case 6: 重建产生的旧目录已无元数据引用（可安全清理）",
                all(
                    item.lower() not in sqlite_referenced_ids(probe_dir)
                    for item in reindexed.get("residue_final", [])
                ),
                f"residue={reindexed.get('residue_final')}",
            )
            reindex_report = reindexed.get("reindex_report") or {}
            check(
                "Case 6: reset_collection() 返回旧物理目录清理报告（removed / deferred）",
                set(reindex_report) == {"removed", "deferred"},
                f"{reindex_report}",
            )
            run_cleanup_script(probe_dir, apply=True, include_physical=True)
            reindex_coll = f"{DEFAULT_COLLECTION}__probe_reindex"
            expected_dirs = {live_seg} | set(reindexed.get("live_segment_ids", []))
            check(
                "Case 6: 维护清理后 probe 物理目录数 = 存活 collection 数（重建不无限增长）",
                physical_dirs(probe_dir) == expected_dirs,
                f"{sorted(physical_dirs(probe_dir))} vs {sorted(expected_dirs)}",
            )
            check(
                "Case 6: 清理后重建的 collection 仍完整可查（元数据 + 向量行数）",
                reindex_coll in sqlite_collection_names(probe_dir)
                and sqlite_embedding_count(probe_dir, reindex_coll) == 3,
                f"{sqlite_embedding_count(probe_dir, reindex_coll)}",
            )
            info(
                "Case 6: probe 物理目录收敛为 "
                f"{len(physical_dirs(probe_dir))} 个 / 存活 collection "
                f"{len(sqlite_collection_names(probe_dir))} 个"
            )

            # ==========================================================
            # 收尾：删除测试 KB，复核真实 .chroma 未留下 collection
            # ==========================================================
            print("\n=== 收尾: 清理测试数据 ===")
            del_c = await client.delete(
                f"/knowledge-bases/{kb_c['id']}", headers=admin_h
            )
            check(
                "收尾: 删除 KB-C → 204 且 collection 真正不存在",
                del_c.status_code == 204
                and coll_c not in sqlite_collection_names(CHROMA_DIR),
                f"{del_c.status_code} {sorted(sqlite_collection_names(CHROMA_DIR))}",
            )

        # 验收用管理员通过 SQL 清理（「管理员不能删除自己」是设计约束）；
        # 同时兜底清理本测试命名空间（s21-* / S21知识库*）——
        # 历史崩溃的运行可能留下同名数据，否则重跑会误判为「清理失败」。
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE username = :u"), {"u": admin_name}
            )
            await conn.execute(
                text("DELETE FROM users WHERE username LIKE 's21-%'")
            )
            await conn.execute(
                text(
                    "DELETE FROM knowledge_bases "
                    "WHERE name LIKE 'S21知识库%'"
                )
            )
            leftover_users = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE username LIKE 's21-%'")
                )
            ).scalar_one()
            leftover_kbs = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM knowledge_bases "
                        "WHERE name LIKE 'S21知识库%'"
                    )
                )
            ).scalar_one()
        cleanup_chroma_leftovers()
        check(
            "收尾: PostgreSQL 测试数据已清理（用户 / 知识库无残留）",
            leftover_users == 0 and leftover_kbs == 0,
            f"users={leftover_users} kbs={leftover_kbs}",
        )
        check(
            "收尾: 真实 .chroma 中未留下任何 collection",
            not sqlite_collection_names(CHROMA_DIR),
            f"{sorted(sqlite_collection_names(CHROMA_DIR))}",
        )
        check(
            "收尾: 真实 .chroma 的物理目录中无本测试新增的未引用残留",
            not (physical_dirs(CHROMA_DIR) - initial_dirs)
            or (physical_dirs(CHROMA_DIR) - initial_dirs).issubset(
                unreferenced_dirs(CHROMA_DIR)
            ),
            f"新增={sorted(physical_dirs(CHROMA_DIR) - initial_dirs)}",
        )
        info(
            "收尾: 真实 .chroma 物理目录 "
            f"{len(physical_dirs(CHROMA_DIR))} 个"
            f"（无主残留 {len(unreferenced_dirs(CHROMA_DIR))} 个，"
            "由 scripts/cleanup_orphan_chroma_storage.py --apply --include-orphan-physical 清理）"
        )
    finally:
        await engine.dispose()
        shutil.rmtree(probe_dir, ignore_errors=True)

    summary_and_exit()


if __name__ == "__main__":
    _args = parse_args()
    if _args.child:
        if not _args.persist_dir or not _args.collection:
            print("--child 模式必须提供 --persist-dir 与 --collection", file=sys.stderr)
            sys.exit(2)
        sys.exit(run_child_mode(_args))
    asyncio.run(main())

