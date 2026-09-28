"""ChromaDB 物理存储维护工具：清理持久化目录中的孤儿数据（Stage 21 引入，长期保留）。

背景
----
ChromaDB 1.x（本项目 1.5.9）的 ``client.delete_collection()`` **只删除元数据**
（``chroma.sqlite3`` 的 ``collections`` / ``segments`` 行），
**不会删除 HNSW 物理目录** ``<persist_dir>/<VECTOR segment id>/``。
平台侧 ``VectorStore.drop_collection()`` 已同时清理物理目录，但 Windows 上被
mmap 占用的目录在「占用进程」退出前谁也删不掉（见末节「Windows 提示」），
因此本工具长期承担两类清理：**历史遗留孤儿** + **运行期延迟残留（deferred）**。

本工具属于正式维护设施（不是一次性脚本），只在维护窗口手动执行。

孤儿判定（只认证据，不猜 UUID）
------------------------------
**A. 逻辑孤儿 collection**（可自动清理）

    名字形如 ``{VECTOR_COLLECTION_NAME}__kb_<整数>``（即本平台的「知识库专属 collection」），
    且 ``<整数>`` 在 PostgreSQL ``knowledge_bases`` 中**不存在**。

**B. 无元数据引用的物理残留**（默认只报告，需显式 ``--include-orphan-physical``）

    持久化目录下名为 UUID 的一级目录，且该名字**不在** ``collections`` /
    ``segments`` 任何 id 中。这类目录无法归属到具体知识库（元数据已被删除），
    但已被 ChromaDB 元数据证明是**无主残留**，删除不会影响任何存活 collection。

安全边界
--------
- 默认 **dry-run**：只打印计划，不删除任何东西（显式 ``--dry-run`` 与默认完全等价）；
  只有 ``--apply`` 才真正执行，二者互斥；
- **默认 collection**（``settings.VECTOR_COLLECTION_NAME``，即不带 ``__kb_`` 后缀的
  那个 ``knowledge_chunks``）永远不会被本脚本清理；只要它存在，其物理目录受
  元数据引用保护，清理逻辑会主动跳过；
- ``--purge-only`` **不做逻辑孤儿判定**（刻意不读 PostgreSQL、跳过第 1/3 步的判定），
  只清理「无元数据引用」的物理残留，因此它**永远不会删除任何 collection**
  （包括逻辑孤儿 collection 本身）；
- 只操作 ``persist_dir`` 下的一级 UUID 目录，不触碰 ``chroma.sqlite3`` 与目录根；
- 任何删除前都会重新读取 ChromaDB 元数据做防御性校验。

用法
----
::

    # 1) 干跑：只查看计划（默认，等价于显式 --dry-run）
    uv run python scripts/cleanup_orphan_chroma_storage.py
    uv run python scripts/cleanup_orphan_chroma_storage.py --dry-run

    # 2) 执行：清理可归属的孤儿 collection（+ 它们的物理目录）
    uv run python scripts/cleanup_orphan_chroma_storage.py --apply

    # 3) 执行：同时清理「无元数据引用」的物理残留
    uv run python scripts/cleanup_orphan_chroma_storage.py --apply --include-orphan-physical

    # 4) 只清理物理残留（不碰任何 collection；适合服务停止后的补充清理）
    uv run python scripts/cleanup_orphan_chroma_storage.py \
        --apply --include-orphan-physical --purge-only

Windows 提示（为什么本脚本要「分进程」执行）
--------------------------------------------
HNSW 文件被「加载过该索引的进程」mmap 占用时无法删除（``WinError 32``）；
而且在占用进程退出之前，**其它任何进程也删不掉**（已实测）。

所以本脚本把职责拆成两半，``--apply`` 一次运行即可收敛：

1. 「删除 collection」（``delete_collection()`` 必然加载该 HNSW）交给**子进程**
   （``--drop-collections``）执行，子进程用完即退，句柄随进程消失；
2. 「删除物理残留」由**父进程**完成，父进程只读 ``chroma.sqlite3`` 枚举集合，
   绝不调用 ``list_collections()`` / ``count()``，因此自身不是占用者。

仍需注意：请在**后端服务已停止**的情况下运行（``--apply``）。否则「服务进程」
持有的 mmap 会让残留删除失败，此时脚本会报告 ``deferred`` 并提示后续处理方式。
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.rag.vectorstore import (  # noqa: E402
    VectorStore,
    collection_names,
    embedding_count,
    hnsw_segment_ids,
    orphan_storage_dirs,
)

DEFAULT_COLLECTION = settings.VECTOR_COLLECTION_NAME

#: 知识库专属 collection 命名规则：``{默认名}__kb_{knowledge_base_id}``
KB_COLLECTION_PATTERN = re.compile(rf"^{re.escape(DEFAULT_COLLECTION)}__kb_(\d+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "清理 ChromaDB 持久化目录中的孤儿数据"
            "（默认 collection 永不清理；默认 dry-run）"
        ),
    )
    parser.add_argument(
        "--persist-dir",
        default=settings.VECTOR_STORE_DIR,
        help=f"ChromaDB 持久化目录（默认 {settings.VECTOR_STORE_DIR}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "只打印计划、不删除任何内容（**与不加 --apply 等价**；"
            "显式传入便于验收脚本 / 流水线表达意图；与 --apply 互斥）"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行删除（不加此参数时只打印计划，不删除任何内容）",
    )
    parser.add_argument(
        "--include-orphan-physical",
        action="store_true",
        help=(
            "同时清理「无元数据引用」的物理残留目录（无法归属到具体知识库，"
            "但已被元数据证明是残留）；仅在 --apply 时生效"
        ),
    )
    parser.add_argument(
        "--purge-only",
        action="store_true",
        help=(
            "（内部/进阶）只清理无元数据引用的物理残留，不再尝试删除任何 collection。"
            "用于服务停止后单独清理残留："
            "--apply --include-orphan-physical --purge-only"
        ),
    )
    parser.add_argument(
        "--drop-collections",
        default="",
        help=(
            "（内部）逗号分隔的 collection 名称：在**本进程**中删除它们的元数据。"
            "父进程以此参数启动子进程来执行「删 collection」这一步，"
            "从而让自己保持「未加载任何 HNSW」的干净状态，得以删除物理残留"
        ),
    )
    return parser.parse_args()


def drop_orphan_collections_in_child_process(
    persist_dir: str, names: list[str]
) -> int:
    """在**独立子进程**中删除逻辑孤儿 collection 的元数据。

    为什么必须分进程：
    ``delete_collection()`` 会让当前进程加载该 collection 的 HNSW 索引；
    HNSW 文件一旦被某进程 mmap，在**该进程存活期间**任何进程都无法删除
    （Windows ``WinError 32``）。所以「删除 collection（必然加载索引）」必须交给
    一个用完即退出的子进程，而「删除物理残留」由不加载任何索引的父进程完成 ——
    这正是真实运行时的形态（服务进程索引并退出后，残留才可被清理）。

    Returns:
        int: 子进程退出码（0 表示子进程成功删除元数据）。
    """
    command = [
        sys.executable,
        os.path.abspath(__file__),
        "--persist-dir",
        persist_dir,
        "--apply",
        "--drop-collections",
        ",".join(names),
    ]
    print("\n" + "=" * 74)
    print(f"在独立子进程中删除 {len(names)} 个逻辑孤儿 collection（元数据）")
    print(f"  {' '.join(command)}")
    print("=" * 74)
    try:
        # 不捕获输出：子进程的 stdout/stderr 直接透传（便于日志与自动化断言）
        return subprocess.run(command, check=False).returncode
    except OSError as exc:  # noqa: BLE001 - 子进程启动失败不应隐藏主结论
        print(f"  ! 子进程启动失败: {exc!r}")
        return -1


async def load_knowledge_base_ids() -> set[int]:
    """读取 PostgreSQL 中仍然存在的知识库 id（孤儿判定的唯一依据）。"""
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id FROM knowledge_bases"))
        return {int(row[0]) for row in rows}


def load_chroma_collections(persist_dir: str) -> dict[str, int]:
    """列出 ChromaDB collection 及其向量行数（**只读元数据，不加载索引**）。

    刻意不使用 ``chromadb`` 官方枚举 API（``list_collections()`` + ``count()``）：
    它会实例化 collection / segment，把 HNSW 文件 mmap 到本进程，
    从而使本进程成为「占用者」，导致后续物理残留一个都删不掉（Windows ``WinError 32``）。
    详见 ``app.rag.vectorstore`` 模块文档「维护脚本为什么也不能用官方枚举 API」。
    """
    return {
        name: embedding_count(persist_dir, name)
        for name in collection_names(persist_dir)
    }


def drop_collections_in_process(args: argparse.Namespace, persist_dir: str) -> int:
    """子进程模式：删除 ``--drop-collections`` 指定的 collection 元数据（不重建）。

    只做「删元数据」这一步；被本进程加载过的 HNSW 目录会在本进程退出后
    由父进程清理（本进程删除必然失败，报告里体现为 ``deferred``）。
    """
    if not args.apply:
        print("`--drop-collections` 必须与 `--apply` 一起使用")
        return 2
    names = [name for name in args.drop_collections.split(",") if name]
    print("=" * 74)
    print(f"（子进程）删除 {len(names)} 个 collection 的元数据")
    print(f"  持久化目录 : {persist_dir}")
    print("=" * 74)
    for name in names:
        report = VectorStore(
            persist_dir=persist_dir, collection_name=name
        ).drop_collection()
        print(f"  - 已删除 collection: {name}")
        print(
            f"      物理目录 removed={report['removed']} "
            f"deferred={report['deferred']}"
            "（本进程加载了索引，延迟项由父进程清理）"
        )
    print("\n（子进程）完成，进程退出即释放 HNSW 句柄。")
    return 0


async def main() -> int:
    args = parse_args()
    persist_dir = str(Path(args.persist_dir))

    if args.apply and args.dry_run:
        print("`--apply` 与 `--dry-run` 互斥：请只选其一（默认即 dry-run）")
        return 2

    if args.drop_collections:
        return drop_collections_in_process(args, persist_dir)

    print("=" * 74)
    print("ChromaDB 孤儿存储清理")
    print(f"  持久化目录 : {persist_dir}")
    print(f"  默认 collection（受保护）: {DEFAULT_COLLECTION}")
    print(
        "  模式       : "
        f"{'APPLY（会真正删除）' if args.apply else 'DRY-RUN（不删除任何内容）'}"
    )
    if args.include_orphan_physical:
        print("  额外选项   : 清理无元数据引用的物理残留")
    print("=" * 74)

    # ---------------- 第 1 步：PostgreSQL 侧事实 ----------------
    if args.purge_only:
        # 子进程只被交接来做物理清理：不读 PG，避免子进程因数据库不可用而失败
        kb_ids: set[int] = set()
        print("\n[1/4] PostgreSQL knowledge_bases: （--purge-only：跳过，仅清理物理残留）")
    else:
        kb_ids = await load_knowledge_base_ids()
        await engine.dispose()
        print(f"\n[1/4] PostgreSQL knowledge_bases: {sorted(kb_ids) or '（空）'}")

    # ---------------- 第 2 步：ChromaDB 侧事实 ----------------
    collections = load_chroma_collections(persist_dir)
    print(f"[2/4] ChromaDB collections: {collections or '（空）'}")

    # ---------------- 第 3 步：孤儿判定 ----------------
    logical_orphans: list[tuple[str, int, list[str]]] = []
    # ``--purge-only`` 刻意不读 PostgreSQL（第 1 步），此时**没有**判定逻辑孤儿的事实依据：
    # 若照旧按「PG 无对应 KB」判定，会把存活知识库的 collection 误报为孤儿，
    # 因此本节直接跳过判定（只清理物理残留，语义与行为保持一致）。
    if not args.purge_only:
        for name in sorted(collections):
            match = KB_COLLECTION_PATTERN.match(name)
            if match is None:
                continue  # 非「知识库专属 collection」→ 不在本脚本清理范围
            knowledge_base_id = int(match.group(1))
            if knowledge_base_id in kb_ids:
                continue  # 知识库仍然存在 → 不是孤儿
            logical_orphans.append(
                (name, knowledge_base_id, hnsw_segment_ids(persist_dir, name))
            )

    physical_orphans = orphan_storage_dirs(persist_dir)

    print("\n[3/4] 孤儿判定结果")
    if args.purge_only:
        print(
            "  A. 逻辑孤儿 collection: 未判定"
            "（--purge-only：不读 PostgreSQL，跳过判定，"
            "避免把存活知识库的 collection 误报为孤儿）"
        )
    else:
        print(
            f"  A. 逻辑孤儿 collection（{DEFAULT_COLLECTION}__kb_* 且 PG 无对应 KB）"
            f": {len(logical_orphans)} 个"
        )
        for name, kb_id, dirs in logical_orphans:
            print(f"       - {name}  (knowledge_base_id={kb_id} 已不存在)")
            print(f"         物理目录: {dirs or '（无）'}")

    print(
        f"  B. 无元数据引用的物理残留: {len(physical_orphans)} 个"
        f"{'' if args.include_orphan_physical else '（需 --include-orphan-physical 才清理）'}"
    )
    for path in physical_orphans:
        print(f"       - {path.name}")

    default_exists = DEFAULT_COLLECTION in collections
    print(
        f"  受保护: 默认 collection {DEFAULT_COLLECTION} "
        + (
            "存在 → 其物理目录受元数据引用保护，绝不清理"
            if default_exists
            else "不存在（无需保护）"
        )
    )

    # ---------------- 第 4 步：执行 ----------------
    if not args.apply:
        print("\n[4/4] DRY-RUN：未删除任何内容。")
        print(
            "      如需执行：加上 --apply"
            "（物理残留需再加 --include-orphan-physical）"
        )
        return 0

    print("\n[4/4] 开始执行清理（--apply）")
    if args.purge_only:
        print("  - --purge-only：跳过 collection 逻辑孤儿判定，只清理物理残留")

    # 删除 collection 会加载其 HNSW（父进程一旦加载就成为「占用者」），
    # 因此这一步交给用完即退的子进程；父进程保持干净，随后负责物理清理。
    if logical_orphans and not args.purge_only:
        print(
            f"  - 待删除的逻辑孤儿 collection（{len(logical_orphans)} 个）: "
            + ", ".join(name for name, _kb, _d in logical_orphans)
        )
        child_exit = drop_orphan_collections_in_child_process(
            persist_dir, [name for name, _kb, _d in logical_orphans]
        )
        print(
            f"  - 子进程退出码: {child_exit}"
            f"（{'成功' if child_exit == 0 else '异常'}）"
        )
        remaining_names = collection_names(persist_dir)
        still_there = [
            name for name, _kb, _d in logical_orphans if name in remaining_names
        ]
        print(
            "  - 子进程删除后仍存在的逻辑孤儿 collection: "
            f"{still_there or '（无）'}"
        )

    if args.include_orphan_physical:
        report = VectorStore(persist_dir=persist_dir).purge_orphan_storage()
        print(
            "  - 已清理无主物理残留: "
            f"removed={len(report['removed'])} deferred={len(report['deferred'])}"
        )
        if report["removed"]:
            print(f"      removed: {report['removed']}")
        if report["deferred"]:
            print(f"      deferred: {report['deferred']}")
    else:
        remaining = orphan_storage_dirs(persist_dir)
        if remaining:
            print(
                f"  - 仍有 {len(remaining)} 个无主物理残留未处理"
                "（如需清理请加 --include-orphan-physical）"
            )

    # ---------------- 复核 ----------------
    final_collections = load_chroma_collections(persist_dir)
    final_orphans = orphan_storage_dirs(persist_dir)
    print("\n复核:")
    print(f"  ChromaDB collections: {final_collections or '（空）'}")
    print(f"  仍存在的无主物理残留: {len(final_orphans)} 个")

    print("\n完成。")
    if final_orphans:
        print(
            f"  ! 仍有 {len(final_orphans)} 个残留未能删除："
            "通常是「其它进程（如后端服务、IDE 索引进程）正持有 mmap」，"
            "被占用进程退出前任何进程都删不掉。"
            "请停止这些进程后重新运行："
            "uv run python scripts/cleanup_orphan_chroma_storage.py "
            "--apply --include-orphan-physical --purge-only"
        )
    else:
        print("  物理残留已全部清理。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
