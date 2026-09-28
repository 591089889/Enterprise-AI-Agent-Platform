"""事务边界（commit 时机）验收测试。

验证目标
--------

    写入数据库 → commit → 响应返回给客户端

即：**写接口返回成功的那一刻，事务已经提交**，紧接着发起的读请求
（新连接 / 新事务）必须立即可见。不允许用 ``sleep`` / 重试掩盖问题。

覆盖
----

1. 注册 → 立即登录            ×10
2. 创建会话 → 立即查询         ×10
3. 创建会话 → 立即 PATCH       ×10
4. 创建会话 → 立即 DELETE      ×10（并验证删除后立即 404）
5. 提交 / 响应事件顺序（进程内、确定性）：``db_commit`` 严格早于
   ``http.response.start``
6. 结构检查（进程内，不发请求）
   - ``DbSession`` 由 ``Depends(get_db, scope="function")`` 构成
   - 所有 ``get_db`` 依赖都声明 ``scope="function"``
   - 同一请求内 ``get_db`` 的依赖缓存键一致（只创建一个 Session）
   - ``commit()`` / ``rollback()`` 只出现在 ``database/session.py``
   - 唯一的生成器依赖是 ``get_db``（``yield`` 只在 session.py）
   - 不存在未声明作用域的 ``Depends(get_db)``（必须使用 ``DbSession``）
   - 本脚本不含 ``sleep``（不靠等待掩盖事务时机问题）
7. 清理测试数据（含失败轮次的补救清理）

运行方式（需要真实 HTTP 服务 + 真实数据库）::

    cd backend
    # 终端 A
    uvicorn app.main:app --host 127.0.0.1 --port 8001
    # 终端 B
    python -m scripts.transaction_boundary_acceptance_test

客户端说明
----------

使用 ``http.client`` 的 **keep-alive 单连接**：写请求返回后立即在同一
socket 上发出下一个请求，客户端侧没有任何额外延迟，是能触发该竞态的
最严格时序。仅在「传输层错误（连接被服务端关闭）」时重连一次，
**不会**对非预期 HTTP 状态码做任何重试。
"""

import ast
import asyncio
import http.client
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

from fastapi import Depends
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.dependencies import DbSession, get_db
from app.database.session import engine
from app.main import app

BASE_HOST = "127.0.0.1"
BASE_PORT = 8001

#: 每一组「写后立即读」的轮数（要求 10/10）
ROUNDS = 10

#: 本次运行的唯一标识（避免与历史遗留测试数据冲突）
RUN_ID = f"{int(time.time())}"

PASSWORD = "Test12345!"

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"

passed: list[str] = []
failed: list[str] = []
group_results: dict[str, str] = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


class HttpClient:
    """keep-alive 单连接 HTTP 客户端（写请求后**不等待**即发下一个请求）。

    Notes:
        ``transport_retries`` 只统计「连接被服务端关闭后重连」的次数，
        与 HTTP 状态码无关；若该值较大说明连接不可复用，需关注但不影响
        事务可见性结论。
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._conn: http.client.HTTPConnection | None = None
        self.transport_retries = 0

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(
                self.host, self.port, timeout=30
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        token: str | None = None,
    ) -> tuple[int, dict]:
        """发送请求并返回 (状态码, 响应 JSON)。

        传输层错误（服务端关闭空闲连接）时重连一次后重发；
        不对任何 HTTP 状态码重试。
        """
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        for attempt in range(2):
            conn = self._connection()
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return response.status, payload
            except (http.client.HTTPException, OSError):
                # 传输层失败（连接不可复用）→ 重连一次；与事务时机无关
                self.close()
                if attempt == 1:
                    raise
                self.transport_retries += 1
        raise AssertionError("unreachable")  # pragma: no cover


def record_group(label: str, succeeded: int, total: int) -> None:
    """记录并打印分组结果（形如 ``注册 → 立即登录：10/10``）。"""
    group_results[label] = f"{succeeded}/{total}"
    print(f"  → {label}: {succeeded}/{total}")
    check(f"{label} 全部成功（{succeeded}/{total}）", succeeded == total,
          f"got {succeeded}/{total}")


# ----------------------------------------------------------------------
# 1. 注册 → 立即登录
# ----------------------------------------------------------------------
def test_register_then_login(
    client: HttpClient,
) -> list[tuple[str, str | None]]:
    print("\n[1] 注册 → 立即登录（无等待）")
    succeeded = 0
    accounts: list[tuple[str, str | None]] = []
    for index in range(1, ROUNDS + 1):
        username = f"txb_user_{RUN_ID}_{index}"
        email = f"{username}@example.com"

        status, body = client.request("POST", "/users/", {
            "username": username,
            "email": email,
            "password": PASSWORD,
        })
        if status != 201:
            check(f"[{index}] 注册返回 201", False, f"got {status} {body}")
            continue
        accounts.append((username, None))

        # 紧接着（同一连接、无 sleep）立即登录
        status, body = client.request("POST", "/auth/login", {
            "email": email,
            "password": PASSWORD,
        })
        ok = status == 200 and bool(body.get("access_token"))
        check(f"[{index}] 注册后立即登录成功", ok,
              f"got {status} {body}")
        if ok:
            succeeded += 1
            accounts[-1] = (username, body["access_token"])

    record_group("注册 → 立即登录", succeeded, ROUNDS)
    return accounts


# ----------------------------------------------------------------------
# 2. 创建会话 → 立即查询（含数据库直连可见性）
# ----------------------------------------------------------------------
def test_create_then_get(
    client: HttpClient, token: str
) -> list[int]:
    print("\n[2] 创建会话 → 立即查询（无等待）")
    succeeded = 0
    alive: list[int] = []
    direct_checked = False

    for index in range(1, ROUNDS + 1):
        status, body = client.request(
            "POST", "/conversations", {"title": f"txb-get-{index}"},
            token=token,
        )
        if status != 201 or not body.get("id"):
            check(f"[{index}] 创建会话返回 201", False,
                  f"got {status} {body}")
            continue
        conversation_id = body["id"]

        # 立即查询（同一连接、无 sleep）
        status, body = client.request(
            "GET", f"/conversations/{conversation_id}", token=token
        )
        ok = status == 200 and body.get("id") == conversation_id
        check(f"[{index}] 创建后立即查询成功", ok, f"got {status} {body}")
        if ok:
            succeeded += 1
            alive.append(conversation_id)

        # 第一轮额外做一次「全新数据库连接」直查（不经过应用 Session）
        if not direct_checked and ok:
            direct_checked = True
            visible = asyncio.run(_row_exists(conversation_id))
            check("[1] 201 返回后立即用全新数据库连接可见（已提交）",
                  visible, "row not visible on a fresh connection")

    record_group("创建 → 立即查询", succeeded, ROUNDS)
    return alive


async def _row_exists(conversation_id: int) -> bool:
    """用**独立的临时连接池**直接查询该会话是否存在（证明确实已提交）。

    刻意不使用应用的 ``engine``：应用引擎的连接由应用自己的事件循环持有，
    在脚本里用 ``asyncio.run`` 借用它会造成跨事件循环复用连接。
    """
    probe_engine = create_async_engine(
        settings.DATABASE_URL, poolclass=NullPool
    )
    try:
        async with probe_engine.connect() as conn:
            result = await conn.execute(
                text("select 1 from conversations where id = :cid"),
                {"cid": conversation_id},
            )
            return result.scalar_one_or_none() == 1
    finally:
        await probe_engine.dispose()


# ----------------------------------------------------------------------
# 3. 创建会话 → 立即 PATCH
# ----------------------------------------------------------------------
def test_create_then_patch(client: HttpClient, token: str) -> list[int]:
    print("\n[3] 创建会话 → 立即 PATCH（无等待）")
    succeeded = 0
    alive: list[int] = []

    for index in range(1, ROUNDS + 1):
        new_title = f"txb-patched-{index}"
        status, body = client.request(
            "POST", "/conversations", {"title": f"txb-patch-{index}"},
            token=token,
        )
        if status != 201 or not body.get("id"):
            check(f"[{index}] 创建会话返回 201", False,
                  f"got {status} {body}")
            continue
        conversation_id = body["id"]

        # 立即重命名（同一连接、无 sleep）
        status, body = client.request(
            "PATCH", f"/conversations/{conversation_id}",
            {"title": new_title}, token=token,
        )
        ok = status == 200 and body.get("title") == new_title
        check(f"[{index}] 创建后立即 PATCH 成功", ok, f"got {status} {body}")
        if ok:
            succeeded += 1
            alive.append(conversation_id)

    record_group("创建 → 立即 PATCH", succeeded, ROUNDS)
    return alive


# ----------------------------------------------------------------------
# 4. 创建会话 → 立即 DELETE
# ----------------------------------------------------------------------
def test_create_then_delete(client: HttpClient, token: str) -> None:
    print("\n[4] 创建会话 → 立即 DELETE（无等待）")
    succeeded = 0

    for index in range(1, ROUNDS + 1):
        status, body = client.request(
            "POST", "/conversations", {"title": f"txb-del-{index}"},
            token=token,
        )
        if status != 201 or not body.get("id"):
            check(f"[{index}] 创建会话返回 201", False,
                  f"got {status} {body}")
            continue
        conversation_id = body["id"]

        # 立即删除（同一连接、无 sleep）
        status, _ = client.request(
            "DELETE", f"/conversations/{conversation_id}", token=token
        )
        ok = status == 204
        check(f"[{index}] 创建后立即 DELETE 成功", ok, f"got {status}")
        if ok:
            succeeded += 1

        # 删除也必须立即可见：马上去查应为 404
        status, body = client.request(
            "GET", f"/conversations/{conversation_id}", token=token
        )
        check(f"[{index}] 删除后立即查询返回 404", status == 404,
              f"got {status} {body}")

    record_group("创建 → 立即 DELETE", succeeded, ROUNDS)


# ----------------------------------------------------------------------
# 5. 事务边界结构检查（进程内，不依赖网络时序）
# ----------------------------------------------------------------------
def _app_sources() -> list[Path]:
    """app/ 下所有 Python 源文件（排除缓存目录）。"""
    return sorted(
        path for path in APP_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _relative(path: Path) -> str:
    """相对 backend/ 的 POSIX 风格路径（用于断言输出）。"""
    return path.relative_to(BACKEND_DIR).as_posix()


def _iter_api_routes(routes) -> "Iterator[object]":
    """遍历路由列表，递归展开 ``include_router`` 产生的路由容器。

    FastAPI 会把 ``include_router`` 的结果包成 ``_IncludedRouter``
    （持有 ``original_router``），因此需要展开后才能拿到真正的
    ``APIRoute``（其 ``dependant`` 才是运行时依赖树）。
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_api_routes(original.routes)
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from _iter_api_routes(nested)


def _collect_get_db_dependants(dependant) -> list:
    """递归收集依赖树中所有 ``get_db`` 子依赖。"""
    found = []
    for sub in dependant.dependencies:
        if sub.call is get_db:
            found.append(sub)
        found.extend(_collect_get_db_dependants(sub))
    return found


def _transaction_calls(tree: ast.AST) -> list[str]:
    """AST 中真实发生的 ``*.commit()`` / ``*.rollback()``（忽略注释/文档）。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"commit", "rollback"}:
                names.append(node.func.attr)
    return names


def _generator_functions(tree: ast.AST) -> list[str]:
    """源码中所有生成器函数名（``yield``）。

    排除 ASGI ``@asynccontextmanager``（如 ``lifespan``）——它们不是
    FastAPI 依赖，只负责应用启动/关闭。
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = {
            getattr(decorator, "id", None)
            or getattr(decorator, "attr", None)
            for decorator in node.decorator_list
        }
        if "asynccontextmanager" in decorators:
            continue
        if any(
            isinstance(child, (ast.Yield, ast.YieldFrom))
            for child in ast.walk(node)
        ):
            names.append(node.name)
    return names


def _has_unscoped_get_db_depends(tree: ast.AST) -> bool:
    """是否存在未声明 ``scope`` 的 ``Depends(get_db)``（会绕过统一作用域）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_depends = (
            isinstance(func, ast.Attribute) and func.attr == "Depends"
        ) or (isinstance(func, ast.Name) and func.id == "Depends")
        passes_get_db = any(
            isinstance(arg, ast.Name) and arg.id == "get_db"
            for arg in node.args
        )
        declares_scope = any(keyword.arg == "scope" for keyword in node.keywords)
        if is_depends and passes_get_db and not declares_scope:
            return True
    return False


def _has_sleep_call(tree: ast.AST) -> bool:
    """是否存在 ``sleep(...)`` 调用（AST 检查，避免字符串自引用）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "sleep":
            return True
    return False


# ----------------------------------------------------------------------
# 5. 「commit 早于响应发送」事件顺序（进程内，确定性检查）
# ----------------------------------------------------------------------
def test_commit_precedes_response() -> None:
    """直接断言事件顺序：DBAPI ``commit`` 严格早于 ASGI 响应发送。

    与网络时序无关，因此不依赖竞态概率：
    - 监听 SQLAlchemy 引擎的 ``commit`` 事件（真实 DBAPI COMMIT）；
    - 用 ASGI 包装层记录 ``http.response.start``（响应字节开始发出）。
    """
    print("\n[5] 提交 / 响应事件顺序（进程内）")
    events: list[str] = []

    def _on_commit(conn) -> None:  # noqa: ANN001
        events.append("db_commit")

    async def instrumented_app(scope, receive, send):  # noqa: ANN001
        async def send_wrapper(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, send_wrapper)

    username = f"txb_order_{RUN_ID}"
    email = f"{username}@example.com"
    event.listen(engine.sync_engine, "commit", _on_commit)
    try:
        client = TestClient(instrumented_app, raise_server_exceptions=False)
        with client:
            response = client.post("/users/", json={
                "username": username,
                "email": email,
                "password": PASSWORD,
            })
            check("写请求返回 201", response.status_code == 201,
                  f"got {response.status_code} {response.text[:120]}")

            # 立即登录（同一进程、无等待）：若提交晚于响应则会读到旧状态
            login = client.post("/auth/login", json={
                "email": email,
                "password": PASSWORD,
            })
            check("写请求返回后立即登录成功", login.status_code == 200,
                  f"got {login.status_code} {login.text[:120]}")

            token = login.json().get("access_token")
            user_id = response.json().get("id")
            if token and user_id:
                status = client.delete(
                    f"/users/{user_id}",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                check("清理顺序验证用户", status == 204, f"got {status}")
    finally:
        event.remove(engine.sync_engine, "commit", _on_commit)

    order_ok = (
        "db_commit" in events
        and "response_start" in events
        and events.index("db_commit") < events.index("response_start")
    )
    check("事件顺序为 db_commit → response_start（commit 先于响应发出）",
          order_ok, f"got {events}")


def test_transaction_boundary_structure() -> None:
    print("\n[6] 事务边界结构检查（进程内）")

    # 5.1 DbSession 元数据：必须是 get_db 且显式声明 function 作用域
    depends_metadata = [
        item for item in get_args(DbSession) if isinstance(item, DependsParam)
    ]
    check("DbSession 由 Depends(get_db) 构成", len(depends_metadata) == 1
          and depends_metadata[0].dependency is get_db,
          f"got {depends_metadata}")
    check('DbSession 声明的依赖作用域是 "function"（响应发出前提交）',
          len(depends_metadata) == 1
          and depends_metadata[0].scope == "function",
          f"got {[item.scope for item in depends_metadata]}")

    # 5.2 运行时依赖树：所有 get_db 依赖都必须是 function 作用域
    dependants = []
    for route in _iter_api_routes(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            dependants.extend(_collect_get_db_dependants(dependant))
    check("应用路由中确实存在 get_db 依赖（检查有效）",
          len(dependants) > 0, f"got {len(dependants)}")

    scopes = sorted({dependant.scope for dependant in dependants})
    check('所有 get_db 依赖都声明 scope="function"（响应发出前提交）',
          scopes == ["function"], f"got {scopes}")

    identities = {
        (dependant.call, dependant.scope, dependant.use_cache)
        for dependant in dependants
    }
    check("同一请求内 get_db 依赖缓存键一致（只会创建一个 Session）",
          len(identities) == 1, f"got {len(identities)}")

    # 5.3 源码级检查（覆盖尚未进入运行时依赖树的代码）
    transaction_files: set[str] = set()
    generator_functions: list[str] = []
    unscoped_get_db: list[str] = []
    for path in _app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _transaction_calls(tree):
            transaction_files.add(_relative(path))
        generator_functions.extend(
            f"{_relative(path)}:{name}" for name in _generator_functions(tree)
        )
        if _has_unscoped_get_db_depends(tree):
            unscoped_get_db.append(_relative(path))

    check("commit / rollback 只出现在 database/session.py（事务边界未分散）",
          transaction_files == {"app/database/session.py"},
          f"got {sorted(transaction_files)}")
    check("唯一的事务生成器依赖是 database/session.py:get_db",
          generator_functions == ["app/database/session.py:get_db"],
          f"got {generator_functions}")
    check("不存在未声明作用域的 Depends(get_db)（统一使用 DbSession）",
          not unscoped_get_db, f"got {sorted(set(unscoped_get_db))}")
    check("Repository / Service / Router 均不自行 commit / rollback",
          not any(
              name.startswith(
                  ("app/repositories/", "app/services/", "app/api/")
              )
              for name in transaction_files
          ))
    check("测试脚本不含 sleep（不靠等待掩盖事务时机问题）",
          not _has_sleep_call(ast.parse(
              Path(__file__).read_text(encoding="utf-8")
          )))


# ----------------------------------------------------------------------
# 6. 清理测试数据
# ----------------------------------------------------------------------
def cleanup(
    client: HttpClient,
    accounts: list[tuple[str, str | None]],
    alive_conversations: list[int],
) -> None:
    print("\n[7] 清理测试数据")

    tokens = [token for _, token in accounts if token]
    if tokens:
        token = tokens[0]
        for conversation_id in alive_conversations:
            status, _ = client.request(
                "DELETE", f"/conversations/{conversation_id}", token=token
            )
            check(f"清理会话 {conversation_id}", status in (204, 404),
                  f"got {status}")

    leftover_recovered = 0
    for username, token in accounts:
        user_id = None

        if token:
            # 情况一：登录成功 → 直接用该用户自己的 token 删除自己
            status, body = client.request("GET", "/users/me", token=token)
            if status == 200:
                user_id = body.get("id")
            else:
                check(f"清理用户 {username}（读取自身）", False,
                      f"got {status} {body}")
        else:
            # 情况二：该轮登录失败（用例已判失败）→ 仅为了清理数据再做一次
            # 登录，不与任何断言挂钩，避免测试数据残留
            status, body = client.request("POST", "/auth/login", {
                "email": f"{username}@example.com",
                "password": PASSWORD,
            })
            if status == 200:
                token = body.get("access_token")
                leftover_recovered += 1
                status, body = client.request(
                    "GET", "/users/me", token=token
                )
                if status == 200:
                    user_id = body.get("id")

        if user_id is None:
            print(f"  [INFO] 用户 {username} 未能清理（无可用凭证）")
            continue
        status, _ = client.request(
            "DELETE", f"/users/{user_id}", token=token
        )
        check(f"清理用户 {username}", status == 204, f"got {status}")

    if leftover_recovered:
        print(f"  [INFO] 失败轮次残留用户补救清理: {leftover_recovered} 个")

    if tokens:
        status, body = client.request(
            "GET", "/users/me", token=tokens[0]
        )
        check("清理后原 token 失效（401，认证语义不变）",
              status == 401, f"got {status} {body}")


def main() -> None:
    print(f"\n运行标识 RUN_ID={RUN_ID}（用于生成唯一测试数据，测试数据会清理）")
    client = HttpClient(BASE_HOST, BASE_PORT)
    accounts: list[tuple[str, str | None]] = []
    alive_conversations: list[int] = []

    try:
        status, body = client.request("GET", "/")
        check("服务可达（GET / → 200）", status == 200,
              f"got {status} {body}")
        if status != 200:
            print("服务不可达，测试中止：请先启动 uvicorn app.main:app")
            sys.exit(1)

        accounts = test_register_then_login(client)
        tokens = [token for _, token in accounts if token]
        if not tokens:
            print("未能创建任何测试用户，测试中止")
            sys.exit(1)

        owner_token = tokens[0]
        alive_conversations.extend(test_create_then_get(client, owner_token))
        alive_conversations.extend(test_create_then_patch(client, owner_token))
        test_create_then_delete(client, owner_token)
        test_commit_precedes_response()
        test_transaction_boundary_structure()
    finally:
        cleanup(client, accounts, alive_conversations)
        client.close()

    print("\n" + "=" * 60)
    for label, result in group_results.items():
        print(f"{label}: {result}")
    print(f"连接复用重试次数（仅传输层）: {client.transport_retries}")
    print("Transaction boundary acceptance: "
          f"{'PASS' if not failed else 'CHECK'}")
    print(f"通过: {len(passed)}  失败: {len(failed)}")
    if failed:
        print("失败项:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通过！")


if __name__ == "__main__":
    main()
