"""Stage 20：移除默认 RAG 数据验收测试。

验证目标
--------
1. 平台启动不加载默认 RAG、不创建默认 collection；
2. 平台默认对话（``agent_id`` 未指定）保留默认 Provider/Model/Temperature/Tools，
   **但不执行 RAG**；
3. 正式 RAG 完全走前端管理的 Knowledge Base：创建 KB → Document → Index → Chroma；
4. Managed Agent 绑定 KB 后 RAG 仍正常工作；
5. 旧默认数据（``backend/knowledge/*.txt``、默认 ``knowledge_chunks``）不再存在；
6. （Stage 21）Chroma collection 生命周期语义：
   - 首次 Index 创建 collection 且 count > 0（Case 5）；
   - **重新 Index** 复用同一个 collection（reset_collection，不重复创建，Case 9）；
   - **删除 KB** 后该 collection **真正不存在**（drop_collection，Case 10）；
   - 删除后可**重新创建 KB** 并正常索引出新 collection（Case 11）。

测试数据全部清理；不读取 / 不打印任何密钥。
"""

import asyncio
import os
import sqlite3
import sys
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
from app.rag.vectorstore import VectorStore  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
CAPTURED: dict = {}

CHROMA_DIR = Path(settings.VECTOR_STORE_DIR)
KNOWLEDGE_DIR = Path(settings.KNOWLEDGE_DIR)
DEFAULT_COLLECTION = settings.VECTOR_COLLECTION_NAME


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


async def spy_generate(self, messages):  # noqa: ANN001
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate",
        provider=type(self).__name__,
        model=self.model,
        temperature=self.temperature,
        system_prompt=messages[0].get("content") if messages else "",
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=None,
    )
    return "STAGE20-SPY-REPLY"


async def spy_generate_with_tools(self, messages, tools):  # noqa: ANN001
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate_with_tools",
        provider=type(self).__name__,
        model=self.model,
        temperature=self.temperature,
        system_prompt=messages[0].get("content") if messages else "",
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=[t.get("function", {}).get("name") for t in tools],
    )
    return LLMResponse(content="STAGE20-SPY-REPLY")


async def register_user(
    client: httpx.AsyncClient, username: str, email: str, password: str
) -> dict:
    resp = await client.post(
        "/users/",
        json={"username": username, "email": email, "password": password},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def fetch_token(
    client: httpx.AsyncClient, email: str, password: str
) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def db_scalar(query: str, params: dict | None = None):
    """执行一条只读 SQL 并返回标量（用于与数据库交叉校验）。"""
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params or {})).scalar_one()


def sqlite_collections() -> list[str]:
    """只读读取 chroma.sqlite3 中的 collection 名称（不启动 Chroma 客户端）。"""
    db = CHROMA_DIR / "chroma.sqlite3"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("SELECT name FROM collections")]
    finally:
        conn.close()


def sqlite_chunk_counts() -> dict[str, int]:
    """只读读取每个 collection 的 chunk 数（不启动 Chroma 客户端）。

    说明：本 Chroma 版本中 ``embeddings.segment_id`` 指向 METADATA scope 的
    segment，因此这里不按 ``scope`` 过滤。
    """
    db = CHROMA_DIR / "chroma.sqlite3"
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.name AS name, COUNT(e.id) AS n "
            "FROM collections c "
            "LEFT JOIN segments s ON s.collection = c.id "
            "LEFT JOIN embeddings e ON e.segment_id = s.id "
            "GROUP BY c.id, c.name"
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        conn.close()


def chroma_collections() -> list[str]:
    """通过 Chroma 官方 API 列出 collection（用于收尾校验）。"""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return sorted(col.name for col in client.list_collections())


async def main() -> None:  # noqa: C901
    QwenProvider.generate = spy_generate
    QwenProvider.generate_with_tools = spy_generate_with_tools

    # ==============================================================
    # Case 1 / 8：旧默认 RAG 数据已不存在；启动不自动建默认 collection
    # ==============================================================
    print("\n=== Case 1/8: 旧默认 RAG 数据与启动行为 ===")
    check(
        "Case 8: backend/knowledge/ 不再存在（项目无预置业务文档）",
        not KNOWLEDGE_DIR.exists(),
        str(KNOWLEDGE_DIR),
    )
    check(
        "Case 8: scripts/verify_rag_context.py 已移除（其目标行为已删除）",
        not (Path(BACKEND) / "scripts" / "verify_rag_context.py").exists(),
    )

    lifespan_src = (Path(BACKEND) / "app" / "core" / "lifespan.py").read_text(
        encoding="utf-8"
    )
    check(
        "Case 1: 应用启动（lifespan）不含任何 RAG / 索引逻辑",
        not any(
            token in lifespan_src
            for token in (
                "VectorStore",
                "Retriever",
                "build_index",
                "KnowledgeLoader",
                "chromadb",
            )
        ),
        lifespan_src[:160],
    )

    collections_before = sqlite_collections()
    counts_before = sqlite_chunk_counts()
    check(
        "Case 1/8: 不存在默认 collection（knowledge_chunks）",
        DEFAULT_COLLECTION not in collections_before,
        f"{collections_before}",
    )
    check(
        "Case 1/8: 不存在任何带向量的 collection（无预置 / 无残留向量数据）",
        all(count == 0 for count in counts_before.values()),
        f"{counts_before}",
    )
    print(f"[info] 启动时 .chroma chunks = {counts_before}")

    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"s20-admin-{tag}"
    user_name = f"s20-user-{tag}"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://s20.test", timeout=60.0
    ) as client:
        await register_user(
            client, admin_name, f"{admin_name}@example.com", pwd
        )
        await register_user(
            client, user_name, f"{user_name}@example.com", pwd
        )
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role='admin' WHERE username=:u"),
                {"u": admin_name},
            )
        admin_h = {
            "Authorization": "Bearer "
            + await fetch_token(client, f"{admin_name}@example.com", pwd)
        }
        user_h = {
            "Authorization": "Bearer "
            + await fetch_token(client, f"{user_name}@example.com", pwd)
        }
        conversation = (
            await client.post("/conversations", json={}, headers=user_h)
        ).json()

        # ==============================================================
        # Case 2 / 7：平台默认对话（agent_id 未指定）不执行 RAG
        # ==============================================================
        print("\n=== Case 2/7: 平台默认对话（不传 agent_id） ===")
        resp_default = await client.post(
            "/agent/chat",
            json={"conversation_id": conversation["id"], "message": "你好"},
            headers=user_h,
        )
        cap_default = dict(CAPTURED)
        check(
            "Case 2: 不传 agent_id → 200",
            resp_default.status_code == 200,
            f"{resp_default.status_code} {resp_default.text[:200]}",
        )
        check(
            "Case 2: 使用默认 Provider（QwenProvider）",
            cap_default.get("provider") == "QwenProvider",
            str(cap_default.get("provider")),
        )
        check(
            "Case 2: 使用默认 Model（settings.LLM_MODEL）",
            cap_default.get("model") == settings.LLM_MODEL,
            f"{cap_default.get('model')} vs {settings.LLM_MODEL}",
        )
        check(
            "Case 2: 使用默认 Temperature（settings.LLM_TEMPERATURE）",
            abs(
                (cap_default.get("temperature") or -1)
                - settings.LLM_TEMPERATURE
            )
            < 1e-9,
            str(cap_default.get("temperature")),
        )
        check(
            "Case 2: 默认工具全部可用（calculator + datetime）",
            cap_default.get("kind") == "generate_with_tools"
            and set(cap_default.get("tools") or [])
            == {"calculator", "datetime"},
            f"{cap_default.get('kind')} {cap_default.get('tools')}",
        )
        check(
            "Case 2: **默认对话不执行 RAG**（system prompt 无参考资料块）",
            "参考资料" not in (cap_default.get("system_prompt") or ""),
            (cap_default.get("system_prompt") or "")[:160],
        )
        check(
            "Case 7: 无默认 collection 时默认对话仍正常（不 500、不空回复）",
            resp_default.status_code == 200
            and (resp_default.json().get("reply") or "") != "",
            str(resp_default.status_code),
        )

        # ==============================================================
        # Case 3 / 4 / 5：KB → Document → Index → Chroma（前端同一套 API）
        # ==============================================================
        print("\n=== Case 3/4/5: Knowledge Base → Document → Index ===")
        kb_resp = await client.post(
            "/knowledge-bases",
            json={"name": f"S20知识库-{tag}", "description": "stage20 验收"},
            headers=admin_h,
        )
        kb = kb_resp.json()
        check(
            "Case 3: 创建 KB → 201",
            kb_resp.status_code == 201 and isinstance(kb.get("id"), int),
            f"{kb_resp.status_code} {kb_resp.text[:160]}",
        )
        check(
            "Case 3: PostgreSQL knowledge_bases 出现记录",
            await db_scalar(
                "SELECT COUNT(*) FROM knowledge_bases WHERE id = :i",
                {"i": kb["id"]},
            )
            == 1,
        )
        check(
            "Case 3: 创建 KB 不产生该 KB 的 Chroma collection（不自动索引）",
            f"{DEFAULT_COLLECTION}__kb_{kb['id']}" not in sqlite_collections(),
            f"{sqlite_collections()}",
        )

        doc_resp = await client.post(
            f"/knowledge-bases/{kb['id']}/documents",
            json={
                "name": "退款规则.md",
                "content": "企业退款规则：订单支付后7天内可以申请退款，超过7天需人工审核。",
            },
            headers=admin_h,
        )
        document = doc_resp.json()
        check(
            "Case 4: 添加 Document → 201 且 index_status=pending（不自动向量化）",
            doc_resp.status_code == 201
            and document.get("index_status") == "pending",
            f"{doc_resp.status_code} {doc_resp.text[:200]}",
        )
        check(
            "Case 4: PostgreSQL documents 出现记录（正文已落库）",
            await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE id = :i",
                {"i": document["id"]},
            )
            == 1,
        )
        kb_collection = f"{DEFAULT_COLLECTION}__kb_{kb['id']}"
        check(
            "Case 4: 未索引时 Chroma 中无该 KB 的 collection",
            kb_collection not in sqlite_collections(),
            f"{sqlite_collections()}",
        )

        index_resp = await client.post(
            f"/knowledge-bases/{kb['id']}/documents/{document['id']}/index",
            headers=admin_h,
        )
        check(
            "Case 5: Index → status=indexed",
            index_resp.status_code == 200
            and index_resp.json().get("status") == "indexed",
            f"{index_resp.status_code} {index_resp.text[:200]}",
        )
        check(
            "Case 5: Chroma 出现该 KB 专属 collection",
            kb_collection in sqlite_collections(),
            f"{sqlite_collections()}",
        )
        chunk_count = VectorStore(collection_name=kb_collection).count()
        check(
            "Case 5: 向量真实写入（chunk 数 > 0）",
            chunk_count > 0,
            str(chunk_count),
        )
        doc_after = (
            await client.get(
                f"/knowledge-bases/{kb['id']}/documents/{document['id']}",
                headers=admin_h,
            )
        ).json()
        check(
            "Case 5: 文档 index_status 已更新为 indexed",
            doc_after.get("index_status") == "indexed",
            str(doc_after.get("index_status")),
        )

        # ==============================================================
        # Case 9（Stage 21）：重新 Index → 复用同一个 collection（reset_collection）
        # ==============================================================
        print("\n=== Case 9: 重新 Index（reset_collection 语义） ===")
        same_name_before = [
            name for name in sqlite_collections() if name == kb_collection
        ]
        reindex_resp = await client.post(
            f"/knowledge-bases/{kb['id']}/documents/{document['id']}/index",
            headers=admin_h,
        )
        reindex_count = VectorStore(collection_name=kb_collection).count()
        same_name_after = [
            name for name in sqlite_collections() if name == kb_collection
        ]
        check(
            "Case 9: 重新 Index → status=indexed",
            reindex_resp.status_code == 200
            and reindex_resp.json().get("status") == "indexed",
            f"{reindex_resp.status_code} {reindex_resp.text[:200]}",
        )
        check(
            "Case 9: 重新 Index 后 collection 仍然存在且仍有向量（count > 0）",
            reindex_count > 0,
            str(reindex_count),
        )
        check(
            "Case 9: 不会产生重复 collection（同名 collection 始终只有 1 个）",
            len(same_name_before) == 1 and len(same_name_after) == 1,
            f"before={len(same_name_before)} after={len(same_name_after)}",
        )
        check(
            "Case 9: 重新 Index 后 chunk 数与首次一致（无重复写入）",
            reindex_count == chunk_count,
            f"{reindex_count} vs {chunk_count}",
        )

        # ==============================================================
        # Case 6：Managed Agent → Knowledge Base → RAG 仍正常
        # ==============================================================
        print("\n=== Case 6: Managed Agent + KB（RAG 必须继续工作） ===")
        agent_resp = await client.post(
            "/agents",
            json={
                "name": f"S20客服助手-{tag}",
                "description": "stage20 验收",
                "system_prompt": "你是客服助手",
                "provider": "qwen",
                "model": settings.LLM_MODEL,
                "temperature": 0.3,
                "knowledge_base_id": kb["id"],
                "tool_names": ["calculator"],
            },
            headers=admin_h,
        )
        agent = agent_resp.json()
        check(
            "Case 6: 创建并绑定 KB 的 Agent → 201",
            agent_resp.status_code == 201
            and agent.get("knowledge_base_id") == kb["id"],
            f"{agent_resp.status_code} {agent_resp.text[:200]}",
        )
        resp_agent = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": agent["id"],
                "message": "退款规则是什么？",
            },
            headers=user_h,
        )
        cap_agent = dict(CAPTURED)
        check(
            "Case 6: Agent 对话 → 200",
            resp_agent.status_code == 200,
            f"{resp_agent.status_code} {resp_agent.text[:200]}",
        )
        check(
            "Case 6: RAG 生效（KB 内容进入 system prompt）",
            "7天内" in (cap_agent.get("system_prompt") or ""),
            (cap_agent.get("system_prompt") or "")[:200],
        )
        check(
            "Case 6: Agent 配置生效（model / temperature 来自 Agent）",
            cap_agent.get("model") == settings.LLM_MODEL
            and abs((cap_agent.get("temperature") or -1) - 0.3) < 1e-9,
            f"{cap_agent.get('model')} {cap_agent.get('temperature')}",
        )
        check(
            "Case 6: 只暴露该 Agent 绑定的工具（calculator）",
            cap_agent.get("tools") == ["calculator"],
            str(cap_agent.get("tools")),
        )

        resp_default_again = await client.post(
            "/agent/chat",
            json={"conversation_id": conversation["id"], "message": "你好"},
            headers=user_h,
        )
        check(
            "Case 7: 存在 KB collection 后，默认对话仍不执行 RAG",
            resp_default_again.status_code == 200
            and "参考资料" not in (CAPTURED.get("system_prompt") or ""),
            (CAPTURED.get("system_prompt") or "")[:160],
        )

        counts_now = sqlite_chunk_counts()
        check(
            "Case 8: 全程从未创建默认 collection（knowledge_chunks）",
            DEFAULT_COLLECTION not in sqlite_collections(),
            f"{sqlite_collections()}",
        )
        check(
            "Case 8: 唯一带向量的 collection 就是本次验收 KB 的 collection",
            {
                name: count
                for name, count in counts_now.items()
                if count > 0
            }
            == {kb_collection: chunk_count},
            f"{counts_now}",
        )

        # ==============================================================
        # 清理测试数据
        # ==============================================================
        print("\n=== 清理测试数据 / Case 10-11（Stage 21） ===")
        me_user = (await client.get("/users/me", headers=user_h)).json()
        await client.delete(f"/agents/{agent['id']}", headers=admin_h)

        # ---------------- Case 10：删除 KB → collection 真正不存在 ----------------
        await client.delete(f"/knowledge-bases/{kb['id']}", headers=admin_h)
        names_after_delete = sqlite_collections()
        print(f"[after-kb-delete] collections = {names_after_delete}")
        check(
            "Case 10: 删除 KB 后该 collection 真正不存在（不是 count=0 的空集合）",
            kb_collection not in names_after_delete,
            f"{names_after_delete}",
        )
        check(
            "Case 10: 官方 API 复核同一结论（列表中没有该 collection 名）",
            kb_collection not in chroma_collections(),
            f"{chroma_collections()}",
        )

        # ---------------- Case 11：删除后重新创建 KB → 新 collection 正常 ----------------
        kb2 = (
            await client.post(
                "/knowledge-bases",
                json={"name": f"S20知识库2-{tag}", "description": "stage21 重建验证"},
                headers=admin_h,
            )
        ).json()
        doc2 = (
            await client.post(
                f"/knowledge-bases/{kb2['id']}/documents",
                json={
                    "name": "退款规则2.md",
                    "content": "企业退款规则：订单支付后7天内可以申请退款。",
                },
                headers=admin_h,
            )
        ).json()
        index2_resp = await client.post(
            f"/knowledge-bases/{kb2['id']}/documents/{doc2['id']}/index",
            headers=admin_h,
        )
        kb2_collection = f"{DEFAULT_COLLECTION}__kb_{kb2['id']}"
        kb2_count = (
            VectorStore(collection_name=kb2_collection).count()
            if kb2_collection in sqlite_collections()
            else -1
        )
        check(
            "Case 11: 删除旧 KB 后新建 KB 并 Index → 新 collection 正常且含向量",
            index2_resp.status_code == 200
            and index2_resp.json().get("status") == "indexed"
            and kb2_count > 0,
            f"{index2_resp.status_code} "
            f"{index2_resp.json().get('status')} count={kb2_count}",
        )
        check(
            "Case 11: 新旧 KB 的 collection 名互不复用（旧名仍不存在）",
            kb2_collection != kb_collection
            and kb_collection not in sqlite_collections(),
            f"{sqlite_collections()}",
        )
        await client.delete(f"/knowledge-bases/{kb2['id']}", headers=admin_h)
        check(
            "Case 11: 第二个 KB 删除后其 collection 同样真正不存在",
            kb2_collection not in sqlite_collections(),
            f"{sqlite_collections()}",
        )

        await client.delete(
            f"/conversations/{conversation['id']}", headers=user_h
        )
        await client.delete(f"/admin/users/{me_user['id']}", headers=admin_h)

        # 安全网：正常情况下此时已无任何 collection（上面的 drop 已真正删除）
        chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        leftover_collections = [c.name for c in chroma_client.list_collections()]
        for name in leftover_collections:
            chroma_client.delete_collection(name)
            print(f"[cleanup] 兜底清理残留 collection: {name}")
        del chroma_client
        check(
            "清理后不存在任何残留 collection（drop_collection 已真正删除）",
            not leftover_collections,
            f"{leftover_collections}",
        )

    # 验收用管理员通过 SQL 清理（管理员不能删除自己）
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE username LIKE 's20-%'")
        )
        leftovers = {
            "users(s20-*)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM users WHERE username LIKE 's20-%'"
                    )
                )
            ).scalar_one(),
            "agents(S20*)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM agents "
                        "WHERE name LIKE 'S20%'"
                    )
                )
            ).scalar_one(),
            "knowledge_bases(S20*)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM knowledge_bases "
                        "WHERE name LIKE 'S20%'"
                    )
                )
            ).scalar_one(),
        }
    check(
        "验收数据已清理（用户 / Agent / 知识库无残留）",
        all(value == 0 for value in leftovers.values()),
        str(leftovers),
    )

    final_names = sqlite_collections()
    check(
        "清理后 .chroma 中不再有任何 collection（回到最干净状态）",
        final_names == [],
        f"{final_names}",
    )
    final_via_api = chroma_collections()
    check(
        "官方 API 复核：清理后 collection 数为 0",
        final_via_api == [],
        f"{final_via_api}",
    )

    await engine.dispose()
    print("\n" + "=" * 76)
    print("Stage 20 默认 RAG 移除验收汇总")
    print("-" * 76)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 76)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())



