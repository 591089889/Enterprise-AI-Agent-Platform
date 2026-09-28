"""Stage 18：Final Permission Audit（guest / user / admin 全量权限审计）。

设计
----
- in-process ASGI（httpx + ASGITransport）驱动真实 app：真实 JWT 认证、
  真实数据库、真实权限依赖（``get_current_user`` / ``require_admin`` /
  conversation ownership）；
- Provider 层用 spy 替换（返回固定文本）：避免审计过程消耗真实 LLM 额度，
  同时让 chat 类接口能完整走通（不伪造 LLM 结果，只固定其输出）；
- 测试创建的用户 / Agent / 知识库 / 会话在结束时**全部清理**；
- 不读取、不打印任何密钥。

覆盖
----
A. 未登录（guest）全域扫描：除公开路由外，所有受保护接口必须 401；
B. user / admin 逐资源矩阵：用户自助 / Agent / Knowledge Base / Dashboard /
   Conversation / Chat；
C. Conversation ownership 隔离：User B 无法读取 / 修改 / 删除 User A 的会话
   （管理员同样受 ownership 约束）；
D. Agent ↔ Knowledge Base（删除后 SET NULL 不 500）与 Agent ↔ Tool 隔离；
E. 结果速览表：Resource | Guest | User | Admin。
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
from fastapi.routing import APIRoute  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agents.providers.base import LLMResponse  # noqa: E402
from app.agents.providers.ollama import OllamaProvider  # noqa: E402
from app.agents.providers.qwen import QwenProvider  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.main import app  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
MATRIX: list[tuple[str, str, str, str]] = []

#: Provider spy 捕获的最近一次请求 payload（用于 Agent 配置 / RAG / Tool 断言）
CAPTURED: dict = {}

#: 公开路由（无需登录）：注册 / 登录 / 根路径
PUBLIC_ROUTES = {
    ("POST", "/auth/login"),
    ("POST", "/users/"),
    ("GET", "/"),
}

#: 非业务路由（FastAPI 自带文档）
DOC_ROUTES = {
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条审计结果。"""
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


def record(resource: str, guest: int, user: int, admin: int) -> None:
    """记录矩阵一行（结果速览表）。"""
    MATRIX.append((resource, str(guest), str(user), str(admin)))


async def spy_generate(self, messages):  # noqa: ANN001
    """替代 Provider.generate（返回固定文本，不发网络请求）。"""
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate",
        model=self.model,
        temperature=self.temperature,
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=None,
    )
    return "PERMISSION-AUDIT-REPLY"


async def spy_generate_with_tools(self, messages, tools):  # noqa: ANN001
    """替代 Provider.generate_with_tools（返回固定文本，不触发工具循环）。"""
    CAPTURED.clear()
    CAPTURED.update(
        kind="generate_with_tools",
        model=self.model,
        temperature=self.temperature,
        messages=[
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
        ],
        tools=[t.get("function", {}).get("name") for t in tools],
    )
    return LLMResponse(content="PERMISSION-AUDIT-REPLY")


async def register_user(
    client: httpx.AsyncClient, username: str, email: str, password: str
) -> None:
    resp = await client.post(
        "/users/",
        json={"username": username, "email": email, "password": password},
    )
    assert resp.status_code in (200, 201), resp.text


async def promote_admin(username: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET role = 'admin' WHERE username = :u"),
            {"u": username},
        )


async def login(
    client: httpx.AsyncClient, email: str, password: str
) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def headers(token: str | None) -> dict:
    return {} if token is None else {"Authorization": f"Bearer {token}"}


async def main() -> None:  # noqa: C901
    QwenProvider.generate = spy_generate
    QwenProvider.generate_with_tools = spy_generate_with_tools
    OllamaProvider.generate = spy_generate
    OllamaProvider.generate_with_tools = spy_generate_with_tools

    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    names = {
        "admin": f"pa-admin-{tag}",
        "user_a": f"pa-usera-{tag}",
        "user_b": f"pa-userb-{tag}",
        "victim": f"pa-victim-{tag}",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://pa.test", timeout=60.0
    ) as client:
        # ---------------- 账号准备 ----------------
        for name in names.values():
            await register_user(client, name, f"{name}@example.com", pwd)
        await promote_admin(names["admin"])

        admin_h = headers(await login(client, f"{names['admin']}@example.com", pwd))
        user_a_h = headers(await login(client, f"{names['user_a']}@example.com", pwd))
        user_b_h = headers(await login(client, f"{names['user_b']}@example.com", pwd))

        me_a = (await client.get("/users/me", headers=user_a_h)).json()
        me_b = (await client.get("/users/me", headers=user_b_h)).json()
        me_victim = (
            await client.get("/users/me", headers=headers(await login(client, f"{names['victim']}@example.com", pwd)))
        ).json()

        async def audit(
            resource: str,
            method: str,
            path: str,
            expected: tuple[int, int, int],
            body: dict | None = None,
            admin_body: dict | None = None,
        ) -> None:
            """以 guest / 普通用户 / 管理员三种身份探测同一路径并校验状态码。"""
            st_guest = (
                await client.request(method, path, json=body, headers={})
            ).status_code
            st_user = (
                await client.request(method, path, json=body, headers=user_a_h)
            ).status_code
            st_admin = (
                await client.request(
                    method,
                    path,
                    json=admin_body if admin_body is not None else body,
                    headers=admin_h,
                )
            ).status_code
            record(resource, st_guest, st_user, st_admin)
            check(
                f"{resource} → guest/user/admin = {expected}",
                (st_guest, st_user, st_admin) == expected,
                f"got guest={st_guest} user={st_user} admin={st_admin}",
            )

        # ==============================================================
        # A. 未登录（guest）全域扫描
        # ==============================================================
        print("\n=== A. 未登录（guest）全域扫描 ===")
        guarded = 0
        unexpected: list[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.path in DOC_ROUTES:
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                if (method, route.path) in PUBLIC_ROUTES:
                    continue
                target = route.path
                for param in route.param_convertors:
                    target = target.replace(f"{{{param}}}", "999999")
                resp = await client.request(
                    method,
                    target,
                    json={} if method in ("POST", "PATCH", "PUT") else None,
                )
                guarded += 1
                if resp.status_code != 401:
                    unexpected.append(f"{method} {route.path} -> {resp.status_code}")
        check(
            f"未登录访问全部受保护路由（{guarded} 个）→ 401",
            not unexpected,
            f"{unexpected}",
        )

        # 公开路由（无需登录）：注册 / 登录 / 根路径
        check(
            "公开路由 GET / 无需登录 → 200",
            (await client.get("/")).status_code == 200,
        )
        check(
            "公开路由 POST /auth/login 无需登录（错误凭据 → 401，非 403）",
            (
                await client.post(
                    "/auth/login",
                    json={"email": "nobody@example.com", "password": "wrong-pass"},
                )
            ).status_code
            == 401,
        )

        # ==============================================================
        # B. 用户自助接口 / 管理员用户管理
        # ==============================================================
        print("\n=== B. 用户自助 / 管理员用户管理 ===")
        victim_id = me_victim["id"]
        await audit("用户自助 GET /users/me", "GET", "/users/me", (401, 200, 200))
        await audit(
            "用户自助 GET /users/{他人}",
            "GET", f"/users/{victim_id}", (401, 403, 403),
        )
        await audit(
            "用户自助 PATCH /users/{他人}",
            "PATCH", f"/users/{victim_id}", (401, 403, 403),
            body={"username": "pa-hacked"},
        )
        await audit(
            "用户自助 DELETE /users/{他人}",
            "DELETE", f"/users/{victim_id}", (401, 403, 403),
        )
        await audit(
            "用户自助 PATCH /users/{自己}",
            "PATCH", f"/users/{me_a['id']}", (401, 200, 403), body={},
        )
        await audit(
            "管理员 GET /admin/users", "GET", "/admin/users", (401, 403, 200)
        )
        await audit(
            "管理员 GET /admin/users/{id}",
            "GET", f"/admin/users/{victim_id}", (401, 403, 200),
        )
        await audit(
            "管理员 PATCH /admin/users/{id}/role",
            "PATCH", f"/admin/users/{victim_id}/role", (401, 403, 200),
            body={"role": "user"},
        )
        await audit(
            "管理员 DELETE /admin/users/{id}（自删防护由 Service 保证）",
            "DELETE", f"/admin/users/{victim_id}", (401, 403, 204),
        )

        # ==============================================================
        # C. Dashboard
        # ==============================================================
        print("\n=== C. Dashboard ===")
        await audit(
            "Dashboard GET /admin/dashboard",
            "GET", "/admin/dashboard", (401, 403, 200),
        )
        stats = (await client.get("/admin/dashboard", headers=admin_h)).json()
        check(
            "Dashboard 统计字段完整（6 项）",
            set(stats) == {
                "users", "agents", "knowledge_bases",
                "documents", "conversations", "messages",
            },
            f"{stats}",
        )
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
            "Dashboard 数字与数据库直连计数一致（不伪造数据）",
            stats == db_counts,
            f"api={stats} db={db_counts}",
        )

        # ==============================================================
        # D. Agent 配置管理
        # ==============================================================
        print("\n=== D. Agent 配置管理 ===")
        agent_1 = (
            await client.post(
                "/agents",
                json={
                    "name": f"pa-agent1-{tag}",
                    "description": "audit",
                    "system_prompt": "你是审计助手",
                    "provider": "qwen",
                    "model": "qwen-audit-1",
                    "temperature": 0.2,
                    "tool_names": ["calculator"],
                },
                headers=admin_h,
            )
        ).json()
        agent_2 = (
            await client.post(
                "/agents",
                json={
                    "name": f"pa-agent2-{tag}",
                    "description": "audit-no-tools",
                    "provider": "qwen",
                    "model": "qwen-audit-2",
                    "temperature": 0.7,
                    "tool_names": [],
                },
                headers=admin_h,
            )
        ).json()
        await audit(
            "Agent GET /agents/available",
            "GET", "/agents/available", (401, 200, 200),
        )
        await audit("Agent GET /agents", "GET", "/agents", (401, 403, 200))
        await audit(
            "Agent GET /agents/tools", "GET", "/agents/tools", (401, 403, 200)
        )
        await audit(
            "Agent GET /agents/{id}",
            "GET", f"/agents/{agent_1['id']}", (401, 403, 200),
        )
        await audit(
            "Agent PATCH /agents/{id}",
            "PATCH", f"/agents/{agent_1['id']}", (401, 403, 200),
            body={"description": "audit-updated"},
        )
        await audit(
            "Agent DELETE /agents/{id}",
            "DELETE", f"/agents/{agent_2['id']}", (401, 403, 204),
        )

        # ==============================================================
        # E. Knowledge Base 管理
        # ==============================================================
        print("\n=== E. Knowledge Base 管理 ===")
        kb_1 = (
            await client.post(
                "/knowledge-bases",
                json={"name": f"pa-kb1-{tag}", "description": "audit"},
                headers=admin_h,
            )
        ).json()
        doc_1 = (
            await client.post(
                f"/knowledge-bases/{kb_1['id']}/documents",
                json={
                    "name": "退款规则.md",
                    "content": "企业退款规则：订单支付后7天内可以申请退款，超过7天需人工审核。",
                },
                headers=admin_h,
            )
        ).json()
        # 索引（写真实向量库），供后面 RAG 隔离验证使用
        index_resp = await client.post(
            f"/knowledge-bases/{kb_1['id']}/documents/{doc_1['id']}/index",
            headers=admin_h,
        )
        check(
            "KB 文档索引真实成功（status=indexed）",
            index_resp.status_code == 200
            and index_resp.json().get("status") == "indexed",
            f"{index_resp.status_code} {index_resp.text[:200]}",
        )
        doc_2 = (
            await client.post(
                f"/knowledge-bases/{kb_1['id']}/documents",
                json={"name": "待删除.md", "content": "临时文档"},
                headers=admin_h,
            )
        ).json()

        await audit(
            "KB GET /knowledge-bases", "GET", "/knowledge-bases",
            (401, 403, 200),
        )
        resp_kb_post = await client.post(
            "/knowledge-bases", json={"name": f"pa-kb-matrix-{tag}"},
            headers=admin_h,
        )
        st_guest = (
            await client.post("/knowledge-bases", json={"name": "x"}, headers={})
        ).status_code
        st_user = (
            await client.post(
                "/knowledge-bases", json={"name": "x"}, headers=user_a_h
            )
        ).status_code
        record(
            "KB POST /knowledge-bases",
            st_guest, st_user, resp_kb_post.status_code,
        )
        check(
            "KB POST /knowledge-bases → guest/user/admin = (401, 403, 201)",
            (st_guest, st_user, resp_kb_post.status_code) == (401, 403, 201),
            f"got {st_guest}/{st_user}/{resp_kb_post.status_code}",
        )
        kb_matrix_id = resp_kb_post.json()["id"]

        await audit(
            "KB GET /knowledge-bases/{id}",
            "GET", f"/knowledge-bases/{kb_1['id']}", (401, 403, 200),
        )
        await audit(
            "KB PATCH /knowledge-bases/{id}",
            "PATCH", f"/knowledge-bases/{kb_1['id']}", (401, 403, 200),
            body={"description": "audit-updated"},
        )
        await audit(
            "KB GET .../documents",
            "GET", f"/knowledge-bases/{kb_1['id']}/documents",
            (401, 403, 200),
        )
        await audit(
            "KB POST .../documents",
            "POST", f"/knowledge-bases/{kb_1['id']}/documents",
            (401, 403, 201),
            body={"name": "denied.md", "content": "denied"},
            admin_body={"name": "matrix.md", "content": "matrix"},
        )
        await audit(
            "KB GET .../documents/{id}",
            "GET", f"/knowledge-bases/{kb_1['id']}/documents/{doc_1['id']}",
            (401, 403, 200),
        )
        await audit(
            "KB PATCH .../documents/{id}",
            "PATCH", f"/knowledge-bases/{kb_1['id']}/documents/{doc_1['id']}",
            (401, 403, 200),
            # 只改文档名，避免覆盖用于 RAG 断言的正文内容
            body={"name": "退款规则(已更新).md"},
        )
        await audit(
            "KB POST .../documents/{id}/index",
            "POST", f"/knowledge-bases/{kb_1['id']}/documents/{doc_1['id']}/index",
            (401, 403, 200),
        )
        await audit(
            "KB DELETE .../documents/{id}",
            "DELETE", f"/knowledge-bases/{kb_1['id']}/documents/{doc_2['id']}",
            (401, 403, 204),
        )
        await audit(
            "KB DELETE /knowledge-bases/{id}",
            "DELETE", f"/knowledge-bases/{kb_matrix_id}", (401, 403, 204),
        )

        # ==============================================================
        # F. Conversation / ownership 隔离
        # ==============================================================
        print("\n=== F. Conversation / ownership 隔离 ===")
        ca = (
            await client.post("/conversations", json={}, headers=user_a_h)
        ).json()
        cb = (
            await client.post("/conversations", json={}, headers=user_b_h)
        ).json()
        ca_admin = (
            await client.post("/conversations", json={}, headers=admin_h)
        ).json()
        ca_delete = (
            await client.post("/conversations", json={}, headers=user_a_h)
        ).json()
        ca_delete_admin = (
            await client.post("/conversations", json={}, headers=admin_h)
        ).json()

        # User A 在自己会话中发一条带标记的消息（用于内容泄露检查）
        secret_marker = "REFUND-SECRET-A"
        chat_a = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_1["id"],
                "message": f"{secret_marker} 退款规则是什么？",
            },
            headers=user_a_h,
        )
        check(
            "User A 在自己会话中对话 → 200",
            chat_a.status_code == 200,
            f"{chat_a.status_code} {chat_a.text[:200]}",
        )

        # User B / Admin 均不得读取或操作 User A 的会话（ownership 优先于角色）
        for method, path, body in (
            ("GET", f"/conversations/{ca['id']}", None),
            ("PATCH", f"/conversations/{ca['id']}", {"title": "hacked"}),
            ("GET", f"/conversations/{ca['id']}/messages", None),
            ("DELETE", f"/conversations/{ca['id']}", None),
        ):
            resp = await client.request(method, path, json=body, headers=user_b_h)
            check(
                f"ownership: User B {method} User A 的会话 → 403",
                resp.status_code == 403,
                f"{resp.status_code} {resp.text[:120]}",
            )
            check(
                f"ownership: User B {method} 的 403 响应不泄露会话内容",
                secret_marker not in resp.text,
                resp.text[:160],
            )

        for method, path in (
            ("GET", f"/conversations/{ca['id']}"),
            ("GET", f"/conversations/{ca['id']}/messages"),
        ):
            resp = await client.request(method, path, headers=admin_h)
            check(
                f"ownership: 管理员 {method} 他人会话 → 403（角色不越权 ownership）",
                resp.status_code == 403,
                f"{resp.status_code} {resp.text[:120]}",
            )
            check(
                f"ownership: 管理员 {method} 的 403 响应不泄露会话内容",
                secret_marker not in resp.text,
                resp.text[:160],
            )

        resp_chat_b = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_1["id"],
                "message": "偷看他人会话",
            },
            headers=user_b_h,
        )
        check(
            "ownership: User B 向 User A 的会话发消息 → 403",
            resp_chat_b.status_code == 403,
            f"{resp_chat_b.status_code} {resp_chat_b.text[:160]}",
        )
        check(
            "ownership: 越权 chat 的 403 响应不泄露会话内容",
            secret_marker not in resp_chat_b.text,
            resp_chat_b.text[:160],
        )

        # 会话接口矩阵（own conversation：user=本人 200，admin=非本人 403）
        await audit(
            "Conversation GET /conversations",
            "GET", "/conversations", (401, 200, 200),
        )
        await audit(
            "Conversation GET /conversations/{user 自己的会话}",
            "GET", f"/conversations/{ca['id']}", (401, 200, 403),
        )
        await audit(
            "Conversation PATCH /conversations/{user 自己的会话}",
            "PATCH", f"/conversations/{ca['id']}", (401, 200, 403),
            body={"title": "audit-title"},
        )
        await audit(
            "Conversation GET /conversations/{user 自己的会话}/messages",
            "GET", f"/conversations/{ca['id']}/messages", (401, 200, 403),
        )
        # DELETE 需要各自的会话（删除后再次探测会变成 404，因此分开探测）
        st_guest = (
            await client.delete(f"/conversations/{ca_delete['id']}", headers={})
        ).status_code
        st_user = (
            await client.delete(
                f"/conversations/{ca_delete['id']}", headers=user_a_h
            )
        ).status_code
        st_admin = (
            await client.delete(
                f"/conversations/{ca_delete_admin['id']}", headers=admin_h
            )
        ).status_code
        record(
            "Conversation DELETE /conversations/{自己的会话}",
            st_guest, st_user, st_admin,
        )
        check(
            "Conversation DELETE /conversations/{自己的会话} "
            "→ guest/user/admin = (401, 204, 204)",
            (st_guest, st_user, st_admin) == (401, 204, 204),
            f"got {st_guest}/{st_user}/{st_admin}",
        )
        check(
            "Conversation DELETE 他人会话 → 403（ownership）",
            (
                await client.delete(
                    f"/conversations/{ca['id']}", headers=user_b_h
                )
            ).status_code
            == 403,
        )

        # POST /conversations 需要分别捕获 guest/user/admin 的返回
        st_guest = (
            await client.post("/conversations", json={}, headers={})
        ).status_code
        resp_user = await client.post("/conversations", json={}, headers=user_a_h)
        resp_admin = await client.post("/conversations", json={}, headers=admin_h)
        record(
            "Conversation POST /conversations",
            st_guest, resp_user.status_code, resp_admin.status_code,
        )
        check(
            "Conversation POST /conversations → guest/user/admin = (401, 201, 201)",
            (st_guest, resp_user.status_code, resp_admin.status_code)
            == (401, 201, 201),
            f"got {st_guest}/{resp_user.status_code}/{resp_admin.status_code}",
        )
        extra_conversations = [resp_user.json()["id"], resp_admin.json()["id"]]

        # ==============================================================
        # G. Chat 权限 + Agent 配置真实生效
        # ==============================================================
        print("\n=== G. Chat 权限 + Agent 配置生效 ===")
        resp_bind = await client.patch(
            f"/agents/{agent_1['id']}",
            json={"knowledge_base_id": kb_1["id"]},
            headers=admin_h,
        )
        check(
            "Admin 绑定知识库到 Agent（管理操作允许）",
            resp_bind.status_code == 200
            and resp_bind.json()["knowledge_base_id"] == kb_1["id"],
            resp_bind.text[:200],
        )

        resp_guest_chat = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_1["id"],
                "message": "你好",
            },
        )
        resp_user_chat = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_1["id"],
                "message": "退款规则是什么？",
            },
            headers=user_a_h,
        )
        cap_user = dict(CAPTURED)
        resp_admin_chat = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca_admin["id"],
                "agent_id": agent_1["id"],
                "message": "你好",
            },
            headers=admin_h,
        )
        record(
            "Chat POST /agent/chat（自己的会话）",
            resp_guest_chat.status_code,
            resp_user_chat.status_code,
            resp_admin_chat.status_code,
        )
        check(
            "Chat POST /agent/chat → guest/user/admin = (401, 200, 200)",
            (
                resp_guest_chat.status_code,
                resp_user_chat.status_code,
                resp_admin_chat.status_code,
            )
            == (401, 200, 200),
            "got "
            f"{resp_guest_chat.status_code}/"
            f"{resp_user_chat.status_code}/"
            f"{resp_admin_chat.status_code}",
        )
        check(
            "Chat: system prompt 来自 Agent 且只有一条 system 消息",
            cap_user["messages"][0]["role"] == "system"
            and "你是审计助手" in cap_user["messages"][0]["content"]
            and sum(
                1 for m in cap_user["messages"] if m["role"] == "system"
            )
            == 1,
            cap_user["messages"][0]["content"][:120],
        )
        check(
            "Chat: model / temperature 来自 Agent 配置",
            cap_user["model"] == "qwen-audit-1"
            and abs(cap_user["temperature"] - 0.2) < 1e-9,
            f'{cap_user["model"]} / {cap_user["temperature"]}',
        )
        check(
            "Chat: Agent 绑定 KB 的检索内容进入 messages（RAG 生效）",
            "7天内" in cap_user["messages"][0]["content"],
            cap_user["messages"][0]["content"][:200],
        )
        check(
            "Chat: 只暴露该 Agent 绑定的工具（calculator）",
            cap_user["tools"] == ["calculator"],
            str(cap_user["tools"]),
        )

        resp_404 = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": 99999999,
                "message": "hi",
            },
            headers=user_a_h,
        )
        check(
            "Chat: agent_id 不存在 → 404（不是 500）",
            resp_404.status_code == 404,
            f"{resp_404.status_code} {resp_404.text[:120]}",
        )
        resp_deleted = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_2["id"],
                "message": "hi",
            },
            headers=user_a_h,
        )
        check(
            "Chat: 已删除 Agent → 404",
            resp_deleted.status_code == 404,
            f"{resp_deleted.status_code} {resp_deleted.text[:120]}",
        )

        # 无工具 / 无知识库的 Agent（Tool 隔离 + 不误检索）
        agent_3 = (
            await client.post(
                "/agents",
                json={
                    "name": f"pa-agent3-{tag}",
                    "description": "audit-plain",
                    "provider": "qwen",
                    "model": "qwen-audit-3",
                    "temperature": 0.9,
                    "tool_names": [],
                },
                headers=admin_h,
            )
        ).json()
        await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_3["id"],
                "message": "你好",
            },
            headers=user_a_h,
        )
        cap_3 = dict(CAPTURED)
        check(
            "Tool 隔离: tools=[] 的 Agent 不向 LLM 暴露任何工具",
            cap_3["tools"] is None and cap_3["kind"] == "generate",
            f'{cap_3["kind"]} {cap_3["tools"]}',
        )
        check(
            "RAG 隔离: 未绑定 KB 的 Agent 不执行检索",
            "参考资料" not in cap_3["messages"][0]["content"],
            cap_3["messages"][0]["content"][:120],
        )

        # 多轮上下文（Conversation history 完整）
        await client.post(
            "/agent/chat",
            json={
                "conversation_id": cb["id"],
                "agent_id": agent_3["id"],
                "message": "我的名字是 Tom",
            },
            headers=user_b_h,
        )
        await client.post(
            "/agent/chat",
            json={
                "conversation_id": cb["id"],
                "agent_id": agent_3["id"],
                "message": "我叫什么？",
            },
            headers=user_b_h,
        )
        cap_multi = dict(CAPTURED)
        roles = [m["role"] for m in cap_multi["messages"]]
        check(
            "Conversation 多轮上下文 = system/user/assistant/user",
            roles == ["system", "user", "assistant", "user"],
            str(roles),
        )

        # ==============================================================
        # H. 知识库删除 → Agent 绑定置空 → Chat 不 500
        # ==============================================================
        print("\n=== H. 知识库删除后的降级行为 ===")
        resp_del_kb = await client.delete(
            f"/knowledge-bases/{kb_1['id']}", headers=admin_h
        )
        check(
            "Admin 删除知识库 → 204",
            resp_del_kb.status_code == 204,
            f"{resp_del_kb.status_code} {resp_del_kb.text[:120]}",
        )
        resp_agent = await client.get(
            f"/agents/{agent_1['id']}", headers=admin_h
        )
        check(
            "删除知识库后 Agent.knowledge_base_id → NULL（ON DELETE SET NULL）",
            resp_agent.json()["knowledge_base_id"] is None,
            resp_agent.text[:200],
        )
        resp_chat_after = await client.post(
            "/agent/chat",
            json={
                "conversation_id": ca["id"],
                "agent_id": agent_1["id"],
                "message": "退款规则是什么？",
            },
            headers=user_a_h,
        )
        check(
            "知识库删除后 Chat 仍 200（不会 500）",
            resp_chat_after.status_code == 200,
            f"{resp_chat_after.status_code} {resp_chat_after.text[:160]}",
        )
        check(
            "知识库删除后不再注入 RAG context",
            "7天内" not in CAPTURED["messages"][0]["content"],
            CAPTURED["messages"][0]["content"][:160],
        )

        # ==============================================================
        # I. 清理测试数据（不污染开发库）
        # ==============================================================
        print("\n=== I. 清理测试数据 ===")
        for agent_id in (agent_1["id"], agent_3["id"]):
            await client.delete(f"/agents/{agent_id}", headers=admin_h)
        for conv_id in (ca["id"], *extra_conversations[:1]):
            await client.delete(f"/conversations/{conv_id}", headers=user_a_h)
        await client.delete(f"/conversations/{cb['id']}", headers=user_b_h)
        for conv_id in (ca_admin["id"], *extra_conversations[1:]):
            await client.delete(f"/conversations/{conv_id}", headers=admin_h)
        for user_id in (me_a["id"], me_b["id"]):
            resp = await client.delete(
                f"/admin/users/{user_id}", headers=admin_h
            )
            check(
                f"管理员删除测试用户 id={user_id} → 204",
                resp.status_code == 204,
                f"{resp.status_code} {resp.text[:120]}",
            )

    # 测试管理员自身通过 SQL 清理（管理员不能删除自己 —— 这是设计约束）
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE username = :u"),
            {"u": names["admin"]},
        )
        left_users = (
            await conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username LIKE 'pa-%'")
            )
        ).scalar_one()
        left_agents = (
            await conn.execute(
                text("SELECT COUNT(*) FROM agents WHERE name LIKE 'pa-agent%'")
            )
        ).scalar_one()
        left_kbs = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_bases "
                    "WHERE name LIKE 'pa-kb%'"
                )
            )
        ).scalar_one()
        left_tools = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM agent_tools WHERE tool_name "
                    "NOT IN ('calculator', 'datetime')"
                )
            )
        ).scalar_one()
    check(
        "审计数据已清理（users / agents / knowledge_bases 无 pa-* 残留）",
        left_users == 0 and left_agents == 0 and left_kbs == 0,
        f"users={left_users} agents={left_agents} kbs={left_kbs}",
    )
    check(
        "agent_tools 无遗留工具绑定",
        left_tools == 0,
        str(left_tools),
    )

    await engine.dispose()

    print("\n" + "=" * 92)
    print("权限速览表（Resource | Guest | User | Admin）")
    print("-" * 92)
    for resource, st_guest, st_user, st_admin in MATRIX:
        print(f"{resource:<54} | {st_guest:>5} | {st_user:>5} | {st_admin:>5}")
    print("-" * 92)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 92)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())






