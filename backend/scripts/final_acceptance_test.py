"""Stage 18：Final Acceptance（完整用户流程端到端验收）。

对应验收步骤（§32）
-------------------
    1  Admin 登录                          → 2  Dashboard 统计正确（与数据库交叉校验）
    3  Admin 创建知识库「企业客服知识库」      → 4  添加文档「退款规则」
    5  建立索引（status=indexed）            → 6  创建 Agent「企业客服助手」
                                             （qwen / model / 0.2 / system_prompt / KB / calculator）
    7  普通 User 登录（只能看到可用 Agent）    → 8  User 选择该 Agent
    9  创建 Conversation                    → 10 发送「退款规则是什么？」（完整链路）
    11 第二轮（多轮上下文）                   → 12 工具测试（calculator 仅对绑定 Agent 可用）
    13 真实 LLM 端到端（HTTP → 8001，若可用；不可用则 SKIP，绝不伪造）

设计
----
- 步骤 1-12：in-process ASGI + Provider spy（真实认证 / 数据库 / 向量库 / 权限 /
  工具子集 / system prompt 组装，只固定 LLM 输出，保证断言确定且不消耗额度）；
- 步骤 13：真实 HTTP 请求打运行中的后端（真实 LLM）；
- 测试数据全部清理；不读取 / 不打印任何密钥。
"""

import asyncio
import os
import sys
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agents.providers.base import LLMResponse  # noqa: E402
from app.agents.providers.qwen import QwenProvider  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.main import app  # noqa: E402

LIVE_BASE_URL = "http://127.0.0.1:8001"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []
CAPTURED: dict = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


def skip(name: str, reason: str) -> None:
    """记录一条明确跳过（不伪造通过）。"""
    SKIP.append(f"{name} | {reason}")
    print(f"SKIP  {name} | {reason}")


async def spy_generate(self, messages):  # noqa: ANN001
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate",
        provider=type(self).__name__,
        model=self.model,
        temperature=self.temperature,
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=None,
    )
    return "ACCEPTANCE-SPY-REPLY"


async def spy_generate_with_tools(self, messages, tools):  # noqa: ANN001
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate_with_tools",
        provider=type(self).__name__,
        model=self.model,
        temperature=self.temperature,
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=[t.get("function", {}).get("name") for t in tools],
    )
    return LLMResponse(content="ACCEPTANCE-SPY-REPLY")


async def register_user(
    client: httpx.AsyncClient, username: str, email: str, password: str
) -> None:
    resp = await client.post(
        "/users/",
        json={"username": username, "email": email, "password": password},
    )
    assert resp.status_code in (200, 201), resp.text


async def fetch_token(
    client: httpx.AsyncClient, email: str, password: str
) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def main() -> None:  # noqa: C901
    # 保存真实 Provider 实现（Step 13 真实 LLM 端到端时恢复使用）
    original_generate = QwenProvider.generate
    original_generate_with_tools = QwenProvider.generate_with_tools
    QwenProvider.generate = spy_generate
    QwenProvider.generate_with_tools = spy_generate_with_tools

    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"e2e-admin-{tag}"
    user_name = f"e2e-user-{tag}"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://e2e.test", timeout=60.0
    ) as client:
        # ---------------- Step 1: 账号准备 + Admin 登录 ----------------
        print("\n=== Step 1: Admin 登录 ===")
        await register_user(client, admin_name, f"{admin_name}@example.com", pwd)
        await register_user(client, user_name, f"{user_name}@example.com", pwd)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role = 'admin' WHERE username = :u"),
                {"u": admin_name},
            )
        admin_token = await fetch_token(client, f"{admin_name}@example.com", pwd)
        admin_h = {"Authorization": f"Bearer {admin_token}"}
        check("Step 1: Admin 登录成功（JWT 可用）", bool(admin_token))

        user_token = await fetch_token(client, f"{user_name}@example.com", pwd)
        user_h = {"Authorization": f"Bearer {user_token}"}

        # ---------------- Step 2: Dashboard 统计正确 ----------------
        print("\n=== Step 2: Dashboard ===")
        resp_dash = await client.get("/admin/dashboard", headers=admin_h)
        stats = resp_dash.json()
        async with engine.connect() as conn:
            db_counts = {
                table: (
                    await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                ).scalar_one()
                for table in (
                    "users", "agents", "knowledge_bases",
                    "documents", "conversations", "messages",
                )
            }
        check(
            "Step 2: Dashboard 返回 200 且统计与数据库一致（不伪造）",
            resp_dash.status_code == 200 and stats == db_counts,
            f"api={stats} db={db_counts}",
        )

        # ---------------- Step 3-5: KB / Document / Index ----------------
        print("\n=== Step 3-5: 知识库 / 文档 / 索引 ===")
        kb = (
            await client.post(
                "/knowledge-bases",
                json={
                    "name": f"企业客服知识库-{tag}",
                    "description": "Stage 18 最终验收",
                },
                headers=admin_h,
            )
        ).json()
        check(
            "Step 3: Admin 创建知识库「企业客服知识库」",
            isinstance(kb.get("id"), int),
            str(kb)[:200],
        )
        doc = (
            await client.post(
                f"/knowledge-bases/{kb['id']}/documents",
                json={
                    "name": "退款规则",
                    "content": (
                        "企业退款规则：订单支付后7天内可以申请退款；"
                        "超过7天的订单需人工审核后处理。"
                    ),
                },
                headers=admin_h,
            )
        ).json()
        check(
            "Step 4: Admin 添加文档「退款规则」",
            isinstance(doc.get("id"), int),
            str(doc)[:200],
        )
        resp_index = await client.post(
            f"/knowledge-bases/{kb['id']}/documents/{doc['id']}/index",
            headers=admin_h,
        )
        check(
            "Step 5: 建立索引 → status=indexed（真实写入向量库）",
            resp_index.status_code == 200
            and resp_index.json().get("status") == "indexed",
            f"{resp_index.status_code} {resp_index.text[:200]}",
        )

        # ---------------- Step 6: 创建 Agent ----------------
        print("\n=== Step 6: 创建 Agent「企业客服助手」 ===")
        agent = (
            await client.post(
                "/agents",
                json={
                    "name": f"企业客服助手-{tag}",
                    "description": "Stage 18 最终验收 Agent",
                    "system_prompt": "你是专业客服助手，回答必须简洁专业。",
                    "provider": "qwen",
                    "model": settings.LLM_MODEL,
                    "temperature": 0.2,
                    "knowledge_base_id": kb["id"],
                    "tool_names": ["calculator"],
                },
                headers=admin_h,
            )
        ).json()
        check(
            "Step 6: Agent 配置保存正确（provider/model/temperature/KB/tools）",
            agent.get("provider") == "qwen"
            and agent.get("model") == settings.LLM_MODEL
            and abs(agent.get("temperature", 0) - 0.2) < 1e-9
            and agent.get("knowledge_base_id") == kb["id"]
            and agent.get("tool_names") == ["calculator"],
            str(agent)[:300],
        )

        # ---------------- Step 7: 普通 User 的可见范围 ----------------
        print("\n=== Step 7: 普通 User 的可见范围 ===")
        me_user = (await client.get("/users/me", headers=user_h)).json()
        resp_available = await client.get("/agents/available", headers=user_h)
        available = resp_available.json()
        check(
            "Step 7: User 可以看到可用 Agent 列表（含刚创建的 Agent）",
            resp_available.status_code == 200
            and agent["id"] in [item["id"] for item in available],
            f"{resp_available.status_code} {str(available)[:200]}",
        )
        check(
            "Step 7: 可用 Agent 只暴露 id/name/description（不含 system_prompt）",
            bool(available)
            and set(available[0]) == {"id", "name", "description"},
            str(available[:1])[:200],
        )
        check(
            "Step 7: User 访问 Agent Management → 403",
            (await client.get("/agents", headers=user_h)).status_code == 403,
        )
        check(
            "Step 7: User 访问 Knowledge Base Management → 403",
            (
                await client.get("/knowledge-bases", headers=user_h)
            ).status_code
            == 403,
        )
        check(
            "Step 7: User 访问 Dashboard → 403",
            (
                await client.get("/admin/dashboard", headers=user_h)
            ).status_code
            == 403,
        )
        check(
            "Step 7: User 访问 Admin 用户管理 → 403",
            (await client.get("/admin/users", headers=user_h)).status_code == 403,
        )

        # ---------------- Step 8-9: 选择 Agent + 创建会话 ----------------
        print("\n=== Step 8-9: 选择 Agent / 创建会话 ===")
        selected_agent_id = available[0]["id"] if available else agent["id"]
        check(
            "Step 8: User 选中可用 Agent（前端选择器数据源）",
            selected_agent_id == agent["id"],
            f"selected={selected_agent_id} created={agent['id']}",
        )
        resp_conv = await client.post("/conversations", json={}, headers=user_h)
        conversation = resp_conv.json()
        check(
            "Step 9: User 创建会话",
            resp_conv.status_code == 201
            and isinstance(conversation.get("id"), int),
            f"{resp_conv.status_code} {str(conversation)[:120]}",
        )

        # ---------------- Step 10: 完整链路（RAG） ----------------
        print("\n=== Step 10: 发送「退款规则是什么？」（完整链路） ===")
        resp_chat = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": selected_agent_id,
                "message": "退款规则是什么？",
            },
            headers=user_h,
        )
        cap = dict(CAPTURED)
        check(
            "Step 10: POST /agent/chat → 200",
            resp_chat.status_code == 200,
            f"{resp_chat.status_code} {resp_chat.text[:200]}",
        )
        check(
            "Step 10: Provider 使用 Agent 配置（model / temperature）",
            cap.get("model") == settings.LLM_MODEL
            and abs(cap.get("temperature", -1) - 0.2) < 1e-9,
            f'{cap.get("model")} / {cap.get("temperature")}',
        )
        check(
            "Step 10: system 消息来自 Agent 且只有一条",
            cap["messages"][0]["role"] == "system"
            and "你是专业客服助手" in cap["messages"][0]["content"]
            and sum(
                1 for m in cap["messages"] if m["role"] == "system"
            )
            == 1,
            cap["messages"][0]["content"][:150],
        )
        check(
            "Step 10: KB 检索结果进入 messages（Agent → KB → RAG → Runtime）",
            "7天内" in cap["messages"][0]["content"],
            cap["messages"][0]["content"][:300],
        )
        check(
            "Step 10: 只暴露该 Agent 绑定的 calculator",
            cap["tools"] == ["calculator"],
            str(cap["tools"]),
        )

        # ---------------- Step 11: 多轮上下文 ----------------
        print("\n=== Step 11: 多轮上下文 ===")
        await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": selected_agent_id,
                "message": "我的订单号是 12345",
            },
            headers=user_h,
        )
        resp_round2 = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": selected_agent_id,
                "message": "我的订单号是多少？",
            },
            headers=user_h,
        )
        cap_multi = dict(CAPTURED)
        roles = [m["role"] for m in cap_multi["messages"]]
        check(
            "Step 11: 第二轮仍 200（上下文可继续）",
            resp_round2.status_code == 200,
            f"{resp_round2.status_code} {resp_round2.text[:160]}",
        )
        check(
            "Step 11: 上下文顺序 = system/user/assistant/user/assistant/user",
            roles
            == ["system", "user", "assistant", "user", "assistant", "user"],
            str(roles),
        )
        check(
            "Step 11: 历史消息内容完整（含上一轮订单号）",
            cap_multi["messages"][3]["role"] == "user"
            and "12345" in cap_multi["messages"][3]["content"],
            cap_multi["messages"][3]["content"][:120],
        )

        # ---------------- Step 12: 工具可用性 ----------------
        print("\n=== Step 12: 工具可用性（calculator） ===")
        resp_calc = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": selected_agent_id,
                "message": "计算 123 + 456",
            },
            headers=user_h,
        )
        cap_calc = dict(CAPTURED)
        check(
            "Step 12: 绑定 calculator 的 Agent → 请求携带 calculator 工具",
            resp_calc.status_code == 200
            and cap_calc["tools"] == ["calculator"]
            and cap_calc["kind"] == "generate_with_tools",
            f'{cap_calc["kind"]} {cap_calc["tools"]}',
        )

        agent_plain = (
            await client.post(
                "/agents",
                json={
                    "name": f"无工具助手-{tag}",
                    "description": "对比用（不绑定工具与知识库）",
                    "provider": "qwen",
                    "model": settings.LLM_MODEL,
                    "temperature": 0.7,
                    "tool_names": [],
                },
                headers=admin_h,
            )
        ).json()
        resp_plain = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "agent_id": agent_plain["id"],
                "message": "计算 123 + 456",
            },
            headers=user_h,
        )
        cap_plain = dict(CAPTURED)
        check(
            "Step 12: 未绑定工具的 Agent → 不暴露 calculator（纯文本对话）",
            resp_plain.status_code == 200
            and cap_plain["tools"] is None
            and cap_plain["kind"] == "generate",
            f'{cap_plain["kind"]} {cap_plain["tools"]}',
        )
        check(
            "Step 12: 未绑定知识库的 Agent → 不注入 RAG context",
            "7天内" not in cap_plain["messages"][0]["content"],
            cap_plain["messages"][0]["content"][:160],
        )

        # ---------------- Step 13: 真实 LLM 端到端（真实 HTTP） ----------------
        print("\n=== Step 13: 真实 LLM 端到端（真实 HTTP → 8001） ===")
        live_conv_id: int | None = None
        if not settings.LLM_API_KEY:
            skip(
                "Step 13: 真实 LLM 端到端（HTTP）",
                "未配置 LLM_API_KEY（按设计：此时 Provider 返回本地模拟回复）",
            )
        else:
            # 恢复真实 Provider（去掉 spy），走真实网络调用
            QwenProvider.generate = original_generate
            QwenProvider.generate_with_tools = original_generate_with_tools
            try:
                async with httpx.AsyncClient(
                    base_url=LIVE_BASE_URL, timeout=180.0
                ) as live:
                    health = await live.get("/")
                    if health.status_code != 200:
                        skip(
                            "Step 13: 真实 LLM 端到端（HTTP）",
                            f"后端 {LIVE_BASE_URL} 不可达（{health.status_code}）",
                        )
                    else:
                        live_token = await fetch_token(
                            live, f"{user_name}@example.com", pwd
                        )
                        live_h = {"Authorization": f"Bearer {live_token}"}
                        live_conv = (
                            await live.post(
                                "/conversations", json={}, headers=live_h
                            )
                        ).json()
                        live_conv_id = live_conv["id"]
                        resp_live = await live.post(
                            "/agent/chat",
                            json={
                                "conversation_id": live_conv_id,
                                "agent_id": agent["id"],
                                "message": "退款规则是什么？",
                            },
                            headers=live_h,
                        )
                        reply = (
                            resp_live.json().get("reply", "")
                            if resp_live.status_code == 200
                            else ""
                        )
                        is_mock = (
                            "模拟" in reply or "LLM_API_KEY" in reply
                        )
                        check(
                            "Step 13: 真实 HTTP + 真实 LLM → 200 且非模拟回复",
                            resp_live.status_code == 200
                            and reply.strip() != ""
                            and not is_mock,
                            f"{resp_live.status_code} reply={reply[:160]}",
                        )
                        print(f"[live] reply = {reply[:240]}")
                        print(
                            "[live] 命中知识库内容(7天/退款) = "
                            f"{('7天' in reply) or ('退款' in reply)}"
                        )
            finally:
                QwenProvider.generate = spy_generate
                QwenProvider.generate_with_tools = spy_generate_with_tools

        # ---------------- 清理测试数据 ----------------
        print("\n=== 清理测试数据 ===")
        for agent_id in (agent["id"], agent_plain["id"]):
            await client.delete(f"/agents/{agent_id}", headers=admin_h)
        await client.delete(f"/knowledge-bases/{kb['id']}", headers=admin_h)
        await client.delete(
            f"/conversations/{conversation['id']}", headers=user_h
        )
        if live_conv_id is not None:
            await client.delete(
                f"/conversations/{live_conv_id}", headers=user_h
            )
        resp_del_user = await client.delete(
            f"/admin/users/{me_user['id']}", headers=admin_h
        )
        check(
            "清理: 管理员删除验收用普通用户 → 204",
            resp_del_user.status_code == 204,
            f"{resp_del_user.status_code} {resp_del_user.text[:120]}",
        )

    # 验收用管理员自身通过 SQL 清理（「管理员不能删除自己」是设计约束）
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE username = :u"), {"u": admin_name}
        )
        leftovers = {
            "users(e2e-*)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM users WHERE username LIKE 'e2e-%'"
                    )
                )
            ).scalar_one(),
            "agents(验收 Agent)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM agents "
                        "WHERE name LIKE '企业客服助手-%' "
                        "OR name LIKE '无工具助手-%'"
                    )
                )
            ).scalar_one(),
            "knowledge_bases(验收 KB)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM knowledge_bases "
                        "WHERE name LIKE '企业客服知识库-%'"
                    )
                )
            ).scalar_one(),
            "documents(验收 KB 下的文档)": (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM documents d "
                        "JOIN knowledge_bases k "
                        "  ON k.id = d.knowledge_base_id "
                        "WHERE k.name LIKE '企业客服知识库-%'"
                    )
                )
            ).scalar_one(),
        }
    check(
        "验收数据已清理（无用户 / Agent / 知识库 / 文档残留）",
        all(value == 0 for value in leftovers.values()),
        str(leftovers),
    )

    await engine.dispose()

    print("\n" + "=" * 88)
    print("Final Acceptance（Stage 18）汇总")
    print("-" * 88)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}   SKIP = {len(SKIP)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    for name in SKIP:
        print(f"  SKIPPED: {name}")
    print("=" * 88)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())




