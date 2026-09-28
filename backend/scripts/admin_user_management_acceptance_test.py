"""Stage 19：Admin 用户管理验收测试（前端功能对应的后端契约 + 权限）。

为什么这样验证
--------------
本阶段补的是**前端**用户管理界面，它调用的三个接口是既有后端能力：

    frontend/src/api/admin.ts → fetchAdminUsers / updateAdminUserRole / deleteAdminUser

因此本脚本按**前端实际发出的请求**逐项验证后端契约与权限，覆盖：

    Case 1/2/3  Admin 可访问 / User 403 / Guest 401（前端路由守卫为体验层）
    Case 4      GET /admin/users：分页字段正确、数据来自数据库（与 SQL 交叉校验）
    Case 5      PATCH /admin/users/{id}/role：user ⇄ admin，落库 + 列表同步
    Case 6      DELETE /admin/users/{id}：204 → 数据库行消失 → 列表不再返回该用户
    Case 7      普通用户直接调用 /admin/users* → 403（不依赖前端隐藏按钮）
    附加        自改角色 403 / 自删 403 / 非法角色 422 / 目标不存在 404 / 分页边界
    静态检查    前端接线（路由 meta.requiresAdmin、Sidebar、api/store/View 引用）

测试数据全部清理；不读取 / 不打印任何密钥。
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

from app.database.session import engine  # noqa: E402
from app.main import app  # noqa: E402

FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


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


def headers(token: str | None) -> dict:
    return {} if token is None else {"Authorization": f"Bearer {token}"}


async def db_scalar(query: str, params: dict | None = None):
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params or {})).scalar_one()


async def main() -> None:  # noqa: C901
    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"s19-admin-{tag}"
    user_name = f"s19-user-{tag}"
    targets = [f"s19-target{i}-{tag}" for i in range(1, 4)]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://s19.test", timeout=60.0
    ) as client:
        # ---------------- 账号准备 ----------------
        await register_user(
            client, admin_name, f"{admin_name}@example.com", pwd
        )
        user_info = await register_user(
            client, user_name, f"{user_name}@example.com", pwd
        )
        target_ids = []
        for name in targets:
            info = await register_user(client, name, f"{name}@example.com", pwd)
            target_ids.append(info["id"])

        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role = 'admin' WHERE username = :u"),
                {"u": admin_name},
            )

        admin_h = headers(await fetch_token(client, f"{admin_name}@example.com", pwd))
        user_h = headers(await fetch_token(client, f"{user_name}@example.com", pwd))
        admin_id = await db_scalar(
            "SELECT id FROM users WHERE username = :u", {"u": admin_name}
        )

        # ==============================================================
        # Case 1 / 4：Admin 获取用户列表（分页字段 + 数据来自数据库）
        # ==============================================================
        print("\n=== Case 1/4: Admin 获取用户列表 ===")
        resp = await client.get(
            "/admin/users", params={"page": 1, "page_size": 100}, headers=admin_h
        )
        check(
            "Case 1: Admin GET /admin/users → 200",
            resp.status_code == 200,
            f"{resp.status_code} {resp.text[:200]}",
        )
        page_data = resp.json()
        check(
            "Case 4: 响应含分页字段 items/total/page/page_size",
            set(page_data) == {"items", "total", "page", "page_size"},
            str(list(page_data))[:200],
        )
        check(
            "Case 4: 用户对象字段与 UserRead 一致（无密码字段）",
            page_data["items"]
            and set(page_data["items"][0])
            == {"id", "username", "email", "role", "created_at"},
            str(page_data["items"][:1])[:200],
        )

        async with engine.connect() as conn:
            db_rows = (
                await conn.execute(
                    text(
                        "SELECT id, username, email, role FROM users "
                        "ORDER BY id ASC"
                    )
                )
            ).all()
        api_rows = [
            (item["id"], item["username"], item["email"], item["role"])
            for item in page_data["items"]
        ]
        check(
            "Case 4: 列表数据与数据库完全一致（不写死 / 不伪造）",
            api_rows == [tuple(row) for row in db_rows]
            and page_data["total"] == len(db_rows),
            f"api={len(api_rows)} db={len(db_rows)} total={page_data['total']}",
        )

        # ==============================================================
        # Case 2 / 7：普通 User 直接调用管理接口 → 403
        # ==============================================================
        print("\n=== Case 2/7: 普通 User 越权调用 → 403（后端真实边界） ===")
        resp_user_list = await client.get("/admin/users", headers=user_h)
        check(
            "Case 7: User GET /admin/users → 403",
            resp_user_list.status_code == 403,
            f"{resp_user_list.status_code} {resp_user_list.text[:160]}",
        )
        check(
            "Case 7: 403 使用后端统一错误结构且非网络错误文案",
            resp_user_list.json().get("error", {}).get("code")
            == "PERMISSION_DENIED",
            resp_user_list.text[:160],
        )
        resp_user_patch = await client.patch(
            f"/admin/users/{target_ids[0]}/role",
            json={"role": "admin"},
            headers=user_h,
        )
        check(
            "Case 7: User PATCH /admin/users/{{id}}/role → 403",
            resp_user_patch.status_code == 403,
            f"{resp_user_patch.status_code}",
        )
        resp_user_delete = await client.delete(
            f"/admin/users/{target_ids[0]}", headers=user_h
        )
        check(
            "Case 7: User DELETE /admin/users/{{id}} → 403",
            resp_user_delete.status_code == 403,
            f"{resp_user_delete.status_code}",
        )
        check(
            "Case 7: 越权尝试未改动数据库（目标用户仍是 user）",
            await db_scalar(
                "SELECT role FROM users WHERE id = :i", {"i": target_ids[0]}
            )
            == "user",
        )
        check(
            "Case 7: 越权删除未生效（用户仍存在）",
            await db_scalar(
                "SELECT COUNT(*) FROM users WHERE id = :i", {"i": target_ids[0]}
            )
            == 1,
        )

        # ==============================================================
        # Case 3：Guest（未登录）→ 401
        # ==============================================================
        print("\n=== Case 3: 未登录 → 401 ===")
        check(
            "Case 3: Guest GET /admin/users → 401",
            (await client.get("/admin/users")).status_code == 401,
        )
        check(
            "Case 3: Guest PATCH /admin/users/{{id}}/role → 401",
            (
                await client.patch(
                    f"/admin/users/{target_ids[0]}/role",
                    json={"role": "admin"},
                )
            ).status_code
            == 401,
        )
        check(
            "Case 3: Guest DELETE /admin/users/{{id}} → 401",
            (
                await client.delete(f"/admin/users/{target_ids[0]}")
            ).status_code
            == 401,
        )

        # ==============================================================
        # 前置确认：越权测试的用户确实是普通用户（否则 403 无意义）
        # ==============================================================
        check(
            "前置: 越权测试账号角色为 user（与 Admin 形成对照）",
            user_info["role"] == "user",
            str(user_info)[:160],
        )

        # ==============================================================
        # Case 5：修改角色 user → admin → user（落库 + 列表同步）
        # ==============================================================
        print("\n=== Case 5: 修改角色（user ⇄ admin） ===")
        resp_promote = await client.patch(
            f"/admin/users/{target_ids[0]}/role",
            json={"role": "admin"},
            headers=admin_h,
        )
        check(
            "Case 5: PATCH role=admin → 200",
            resp_promote.status_code == 200,
            f"{resp_promote.status_code} {resp_promote.text[:160]}",
        )
        check(
            "Case 5: 响应体为更新后的用户（role=admin）",
            resp_promote.json().get("role") == "admin",
            str(resp_promote.json())[:160],
        )
        check(
            "Case 5: 数据库 role 已写入 admin",
            await db_scalar(
                "SELECT role FROM users WHERE id = :i", {"i": target_ids[0]}
            )
            == "admin",
        )
        listed = (
            await client.get(
                "/admin/users",
                params={"page": 1, "page_size": 100},
                headers=admin_h,
            )
        ).json()
        check(
            "Case 5: 重新拉取列表可见新角色（前端列表同步）",
            next(
                item["role"]
                for item in listed["items"]
                if item["id"] == target_ids[0]
            )
            == "admin",
            str([i for i in listed["items"] if i["id"] == target_ids[0]])[:160],
        )
        resp_demote = await client.patch(
            f"/admin/users/{target_ids[0]}/role",
            json={"role": "user"},
            headers=admin_h,
        )
        check(
            "Case 5: admin → user 回退成功（可双向切换）",
            resp_demote.status_code == 200
            and await db_scalar(
                "SELECT role FROM users WHERE id = :i", {"i": target_ids[0]}
            )
            == "user",
            f"{resp_demote.status_code}",
        )

        # ==============================================================
        # Case 6：删除用户（204 → 落库消失 → 列表同步）
        # ==============================================================
        print("\n=== Case 6: 删除用户 ===")
        resp_delete = await client.delete(
            f"/admin/users/{target_ids[1]}", headers=admin_h
        )
        check(
            "Case 6: DELETE /admin/users/{{id}} → 204（无响应体）",
            resp_delete.status_code == 204,
            f"{resp_delete.status_code} {resp_delete.text[:120]}",
        )
        check(
            "Case 6: 数据库中该用户已消失",
            await db_scalar(
                "SELECT COUNT(*) FROM users WHERE id = :i", {"i": target_ids[1]}
            )
            == 0,
        )
        listed_after = (
            await client.get(
                "/admin/users",
                params={"page": 1, "page_size": 100},
                headers=admin_h,
            )
        ).json()
        check(
            "Case 6: 列表不再返回被删用户（前端列表同步）",
            target_ids[1] not in [item["id"] for item in listed_after["items"]]
            and listed_after["total"] == len(listed_after["items"]),
            f"total={listed_after['total']} items={len(listed_after['items'])}",
        )

        # ==============================================================
        # 附加：后端既有业务规则（前端只做体验层禁用与提示）
        # ==============================================================
        print("\n=== 附加: 自改/自删防护、非法角色、目标不存在 ===")
        resp_self_role = await client.patch(
            f"/admin/users/{admin_id}/role",
            json={"role": "user"},
            headers=admin_h,
        )
        check(
            "附加: 管理员修改自己的角色 → 403（后端强制）",
            resp_self_role.status_code == 403,
            f"{resp_self_role.status_code} {resp_self_role.text[:160]}",
        )
        check(
            "附加: 自改角色失败后数据库未变（仍为 admin）",
            await db_scalar(
                "SELECT role FROM users WHERE id = :i", {"i": admin_id}
            )
            == "admin",
        )
        resp_self_delete = await client.delete(
            f"/admin/users/{admin_id}", headers=admin_h
        )
        check(
            "附加: 管理员删除自己 → 403（后端强制）",
            resp_self_delete.status_code == 403,
            f"{resp_self_delete.status_code} {resp_self_delete.text[:160]}",
        )
        check(
            "附加: 自删失败后管理员仍存在",
            await db_scalar(
                "SELECT COUNT(*) FROM users WHERE id = :i", {"i": admin_id}
            )
            == 1,
        )
        resp_bad_role = await client.patch(
            f"/admin/users/{target_ids[2]}/role",
            json={"role": "superuser"},
            headers=admin_h,
        )
        check(
            "附加: 非法角色值 → 422（Pydantic Literal 校验）",
            resp_bad_role.status_code == 422,
            f"{resp_bad_role.status_code}",
        )
        check(
            "附加: 非法角色请求未改动数据库",
            await db_scalar(
                "SELECT role FROM users WHERE id = :i", {"i": target_ids[2]}
            )
            == "user",
        )
        resp_missing_role = await client.patch(
            "/admin/users/99999999/role",
            json={"role": "admin"},
            headers=admin_h,
        )
        check(
            "附加: 目标用户不存在（改角色）→ 404",
            resp_missing_role.status_code == 404,
            f"{resp_missing_role.status_code}",
        )
        resp_missing_delete = await client.delete(
            "/admin/users/99999999", headers=admin_h
        )
        check(
            "附加: 目标用户不存在（删除）→ 404",
            resp_missing_delete.status_code == 404,
            f"{resp_missing_delete.status_code}",
        )

        # ==============================================================
        # 附加：后端分页（前端沿用，不做客户端全量拉取）
        # ==============================================================
        print("\n=== 附加: 后端分页 ===")
        page1 = (
            await client.get(
                "/admin/users",
                params={"page": 1, "page_size": 2},
                headers=admin_h,
            )
        ).json()
        page2 = (
            await client.get(
                "/admin/users",
                params={"page": 2, "page_size": 2},
                headers=admin_h,
            )
        ).json()
        check(
            "附加: page_size=2 → 第 1 页返回 2 条且回显分页参数",
            len(page1["items"]) == 2
            and page1["page"] == 1
            and page1["page_size"] == 2,
            f"{len(page1['items'])} {page1['page']} {page1['page_size']}",
        )
        total_db = await db_scalar("SELECT COUNT(*) FROM users")
        check(
            "附加: total 等于数据库用户总数",
            page1["total"] == total_db == page2["total"],
            f"api={page1['total']} db={total_db}",
        )
        check(
            "附加: 第 2 页与第 1 页返回不同用户（分页生效）",
            {i["id"] for i in page1["items"]}.isdisjoint(
                {i["id"] for i in page2["items"]}
            ),
            f"p1={[i['id'] for i in page1['items']]} "
            f"p2={[i['id'] for i in page2['items']]}",
        )

        # ==============================================================
        # 前端接线静态检查（本阶段补的是前端；浏览器渲染不在本脚本范围）
        # 说明：这里只验证**代码接线**（路由守卫 / 页面 → store → api），
        #       真实渲染与交互由人工在浏览器确认，不伪造浏览器测试结果。
        # ==============================================================
        print("\n=== 前端接线静态检查 ===")

        def read_frontend(relative: str) -> str:
            with open(os.path.join(FRONTEND, relative), encoding="utf-8") as fh:
                return fh.read()

        router_src = read_frontend(os.path.join("router", "index.ts"))
        sidebar_src = read_frontend(os.path.join("components", "Sidebar.vue"))
        admin_api_src = read_frontend(os.path.join("api", "admin.ts"))
        admin_store_src = read_frontend(os.path.join("stores", "admin.ts"))
        admin_view_src = read_frontend(os.path.join("views", "AdminView.vue"))
        layout_src = read_frontend(os.path.join("layouts", "MainLayout.vue"))

        check(
            "前端: /app/admin 路由带 meta.requiresAdmin（守卫拦截非管理员）",
            "name: 'admin'" in router_src
            and "requiresAdmin: true" in router_src,
        )
        check(
            "前端: Sidebar「系统管理」标记 requiresAdmin（普通用户不可见）",
            "name: 'admin'" in sidebar_src
            and "requiresAdmin: true" in sidebar_src,
        )
        check(
            "前端: api/admin.ts 调用 /admin/users 三个接口",
            all(
                token in admin_api_src
                for token in (
                    "fetchAdminUsers",
                    "updateAdminUserRole",
                    "deleteAdminUser",
                    "'/admin/users'",
                    "`/admin/users/${userId}/role`",
                    "`/admin/users/${userId}`",
                )
            ),
        )
        check(
            "前端: stores/admin.ts 提供 load/changeRole/remove（View→Store→API）",
            all(
                token in admin_store_src
                for token in (
                    "useAdminStore",
                    "fetchAdminUsers",
                    "updateAdminUserRole",
                    "deleteAdminUser",
                    "async function changeRole",
                    "async function remove",
                )
            ),
        )
        check(
            "前端: AdminView 使用 admin store 且删除前二次确认",
            all(
                token in admin_view_src
                for token in (
                    "useAdminStore",
                    "用户管理",
                    "onRoleChange",
                    "onDelete",
                    "window.confirm",
                )
            ),
        )
        check(
            "前端: AdminView 未直接使用 axios（必须经 api/* 分层）",
            "axios" not in admin_view_src,
        )
        check(
            "前端: 登出时重置 admin store（避免状态残留）",
            "admin.reset()" in layout_src,
        )

        # ==============================================================
        # 清理测试数据
        # ==============================================================
        print("\n=== 清理测试数据 ===")
        for user_id in (user_info["id"], *target_ids):
            resp = await client.delete(
                f"/admin/users/{user_id}", headers=admin_h
            )
            print(f"[cleanup] DELETE /admin/users/{user_id} → {resp.status_code}")
        check(
            "清理: 管理员删除自己 → 403（设计约束保持不变）",
            (
                await client.delete(f"/admin/users/{admin_id}", headers=admin_h)
            ).status_code
            == 403,
        )

    # 管理员自身通过 SQL 清理（「管理员不能删除自己」是后端设计约束）
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE username LIKE 's19-%'")
        )
        leftover = (
            await conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username LIKE 's19-%'")
            )
        ).scalar_one()

    check("测试数据已清理（无 s19-* 残留）", leftover == 0, str(leftover))

    async with engine.connect() as conn:
        role_summary = (
            await conn.execute(
                text("SELECT role, COUNT(*) FROM users GROUP BY role ORDER BY role")
            )
        ).all()
    print(f"[final] users by role = {[(r[0], r[1]) for r in role_summary]}")

    await engine.dispose()

    print("\n" + "=" * 74)
    print("Stage 19 Admin 用户管理验收汇总")
    print("-" * 74)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 74)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())




