"""Stage 10：统一异常处理与 API 错误响应规范 验收测试。

覆盖目标
--------

1. ``AppException`` 结构（status_code / code / message）
2. 全局 Exception Handler（AppException → 统一 HTTP 响应）
3. 404（资源不存在，结构与错误码统一）
4. 权限错误（401 / 403 语义不变）
5. Validation Error（保持 HTTP 422 语义）
6. Agent Provider Error（Mock Provider → API 边界统一错误响应）
7. Tool Error Regression（Stage 8 的 Runtime 行为未被改变）
8. 未预期异常 → 500 统一结构（不泄露内部细节）
9. 敏感信息保护（错误响应不含密钥 / password / SQL / traceback）
10. 架构边界静态检查（Router → Service → Runtime → Provider 未变）

运行方式（**不需要**手动启动服务器）::

    cd backend
    python -m scripts.exception_handling_acceptance_test

说明
----

- 使用 FastAPI ``TestClient`` 在进程内运行**真实 app**（真实 Router /
  Service / Runtime / DI / 数据库链路），只对「上游 LLM Provider」做 Mock；
- 不访问真实 LLM API、不使用真实 API Key；
- 需要可连接的数据库（与 Stage 6 HTTP 回归一致），
  测试用户 / 会话在 ``finally`` 中清理；
- 本脚本不打印 backend/.env 中的任何敏感值
  （敏感信息检查只比对配置值是否出现在响应中，不输出值本身）。
"""

import ast
import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.agents.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    parse_chat_completion,
)
from app.agents.runtime import AgentRuntime
from app.agents.tools import (
    ToolError,
    ToolExecutionError,
    ToolParameterError,
    UnknownToolError,
    build_default_registry,
)
from app.api.agent import UPSTREAM_UNAVAILABLE_MESSAGE
from app.core.config import settings
from app.core.dependencies import (
    DbSession,
    get_agent_service,
    get_user_service,
)
from app.core.exception_handlers import (
    INTERNAL_ERROR_MESSAGE,
    app_exception_handler,
)
from app.core.exceptions import (
    ERROR_CODE_FIELD,
    ERROR_FIELD,
    ERROR_MESSAGE_FIELD,
    AppException,
    BusinessConflictException,
    PermissionDeniedException,
    ResourceNotFoundException,
    UnauthorizedException,
    UpstreamServiceException,
    build_error_payload,
)
from app.main import app
from app.services.agent import AgentService

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"

OWNER_USERNAME = "stage10_owner"
OWNER_EMAIL = f"{OWNER_USERNAME}@example.com"
OTHER_USERNAME = "stage10_other"
OTHER_EMAIL = f"{OTHER_USERNAME}@example.com"
TEST_PASSWORD = "Test12345!"

#: 不存在的资源 ID（用于 404 场景）
MISSING_ID = 999_999_999

passed: list[str] = []
failed: list[str] = []

#: 收集所有**错误响应体**原文，供敏感信息扫描使用
error_bodies: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def auth_headers(token: str) -> dict:
    """构造 Bearer Token 请求头。"""
    return {"Authorization": f"Bearer {token}"}


def imported_modules(source: str) -> set[str]:
    """用 AST 提取源码 import 的模块名（不受注释 / 文档字符串干扰）。"""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def error_of(payload: dict) -> dict:
    """取出统一错误结构中的 ``error`` 对象（结构不符时返回空 dict）。"""
    error = payload.get(ERROR_FIELD)
    return error if isinstance(error, dict) else {}


def record_error_response(response) -> dict:
    """记录错误响应体原文（敏感信息扫描用），并返回解析后的 JSON。"""
    error_bodies.append(response.text)
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {}


def check_error_response(
    name: str,
    response,
    status: int,
    code: str,
    message: str | None = None,
) -> dict:
    """断言「状态码 + 统一错误结构 + 错误码（可选具体文案）」全部成立。"""
    error = error_of(record_error_response(response))
    ok = (
        response.status_code == status
        and error.get(ERROR_CODE_FIELD) == code
        and isinstance(error.get(ERROR_MESSAGE_FIELD), str)
        and bool(error.get(ERROR_MESSAGE_FIELD))
    )
    if message is not None:
        ok = ok and error.get(ERROR_MESSAGE_FIELD) == message
    check(name, ok, f"got {response.status_code} {response.text[:200]}")
    return error



# ----------------------------------------------------------------------
# 测试用 Provider（只实现 LLMProvider 抽象，不访问网络 / 不使用真实 Key）
# ----------------------------------------------------------------------
class ExplodingProvider(LLMProvider):
    """模拟上游 LLM 服务故障（网络 / HTTP 错误）。"""

    def __init__(self) -> None:
        self.message = "模拟上游 LLM 网络故障（Stage 10 测试，非真实错误）"

    async def generate(self, messages: list[dict]) -> str:
        raise LLMProviderError(self.message)

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        raise LLMProviderError(self.message)


class MalformedUpstreamProvider(LLMProvider):
    """模拟上游返回无法解析的响应（协议错误，走真实解析层）。"""

    async def generate(self, messages: list[dict]) -> str:
        # 真实 Provider 的解析逻辑：非法响应 → LLMProviderError
        parse_chat_completion({"bad": "payload"}, provider_label="Stage10Mock")
        return "unreachable"  # pragma: no cover - 上面必定抛异常


class ScriptedToolProvider(LLMProvider):
    """按脚本逐轮返回 tool_calls，脚本用完后给出最终回答（不访问网络）。

    Args:
        rounds: 每一轮的 tool_calls 列表；列表用完后返回
            :attr:`FINAL_ANSWER`（模拟模型看到 tool 结果后给出最终回答）。
    """

    FINAL_ANSWER = "已根据工具结果完成回答（Stage 10 Tool 回归）。"

    def __init__(self, rounds: list[list[ToolCall]]) -> None:
        self.rounds = [list(round_) for round_ in rounds]

    async def generate(self, messages: list[dict]) -> str:
        return self.FINAL_ANSWER

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        if self.rounds:
            return LLMResponse(content=None, tool_calls=self.rounds.pop(0))
        return LLMResponse(content=self.FINAL_ANSWER)


def override_agent_service(runtime: AgentRuntime):
    """用测试 Runtime 覆盖 ``get_agent_service``。

    只替换「Provider + Runtime」，数据库 / Conversation / Message
    持久化仍是真实链路（``db`` 由 ``DbSession`` 正常注入，
    与生产一致：``scope="function"``，响应发出前提交）。
    """

    async def _override(
        db: DbSession,
    ) -> AgentService:
        return AgentService(db, runtime, retriever=None)

    return _override


# ----------------------------------------------------------------------
# 1. AppException 结构
# ----------------------------------------------------------------------
def test_exception_types() -> None:
    print("\n[1] AppException 结构（status_code / code / message）")

    cases = [
        (ResourceNotFoundException("会话 ID=42 不存在"), 404,
         "RESOURCE_NOT_FOUND"),
        (BusinessConflictException("用户名已被占用"), 409, "BUSINESS_CONFLICT"),
        (PermissionDeniedException("无权限"), 403, "PERMISSION_DENIED"),
        (UnauthorizedException("Invalid email or password"), 401,
         "AUTHENTICATION_REQUIRED"),
        (UpstreamServiceException("上游不可用"), 502, "UPSTREAM_SERVICE_ERROR"),
    ]
    for exc, expected_status, expected_code in cases:
        check(
            f"{type(exc).__name__}: {expected_status} / {expected_code}",
            exc.status_code == expected_status
            and exc.code == expected_code
            and bool(exc.message),
            f"got {exc.status_code} / {exc.code}",
        )
        check(
            f"{type(exc).__name__} 属于 AppException 异常体系",
            isinstance(exc, AppException),
        )

    base = AppException("通用业务错误")
    check("AppException 基类默认 400 / APP_ERROR",
          base.status_code == 400 and base.code == "APP_ERROR",
          f"got {base.status_code} / {base.code}")

    payload = build_error_payload("RESOURCE_NOT_FOUND", "会话 ID=42 不存在")
    check("统一错误体结构稳定（唯一来源 build_error_payload）",
          payload == {
              "error": {
                  "code": "RESOURCE_NOT_FOUND",
                  "message": "会话 ID=42 不存在",
              }
          },
          f"got {payload}")
    check("错误响应只有 code / message 两个字段（无 trace_id / stack / metadata）",
          set(payload["error"]) == {"code", "message"})

    exceptions_source = (
        APP_DIR / "core" / "exceptions.py"
    ).read_text(encoding="utf-8")
    check("exceptions.py 不导入任何模块（业务异常层保持框架无关）",
          imported_modules(exceptions_source) == set(),
          f"got {sorted(imported_modules(exceptions_source))}")



# ----------------------------------------------------------------------
# 2. 全局 Exception Handler
# ----------------------------------------------------------------------
def test_exception_handlers(client, owner_token: str) -> None:
    print("\n[2] 全局 Exception Handler（AppException → 统一 HTTP 响应）")

    registered = {
        getattr(key, "__name__", str(key)) for key in app.exception_handlers
    }
    check(
        "处理器已注册（AppException / HTTPException / "
        "RequestValidationError / Exception）",
        {"AppException", "HTTPException", "RequestValidationError",
         "Exception"} <= registered,
        f"got {sorted(registered)}",
    )
    check("main.py 注册的 AppException 处理器与实现是同一函数对象",
          app.exception_handlers.get(AppException) is app_exception_handler)

    # 2.1 直接调用 handler（不经过任何 Router）：AppException → JSONResponse
    response = asyncio.run(
        app_exception_handler(None, BusinessConflictException("资源冲突（测试）"))
    )
    payload = json.loads(response.body)
    check(
        "AppException handler → HTTP 409 + 统一结构",
        response.status_code == 409
        and error_of(payload).get(ERROR_CODE_FIELD) == "BUSINESS_CONFLICT"
        and error_of(payload).get(ERROR_MESSAGE_FIELD) == "资源冲突（测试）",
        f"got {response.status_code} {response.body!r}",
    )

    # 2.2 真实端到端：依赖注入层抛出的 AppException 走全局 handler
    response = client.get("/admin/users", headers=auth_headers(owner_token))
    check_error_response(
        "依赖层 PermissionDeniedException → 403 统一结构",
        response, 403, "PERMISSION_DENIED", "Admin privileges required",
    )
    check("403 响应不含 traceback / 内部路径",
          "Traceback" not in response.text
          and "site-packages" not in response.text)


# ----------------------------------------------------------------------
# 3. 404
# ----------------------------------------------------------------------
def test_not_found(client, owner_token: str) -> None:
    print("\n[3] 404 资源不存在")

    response = client.get(
        f"/conversations/{MISSING_ID}", headers=auth_headers(owner_token)
    )
    error = check_error_response(
        "GET /conversations/{不存在} → 404 RESOURCE_NOT_FOUND",
        response, 404, "RESOURCE_NOT_FOUND",
    )
    check("404 message 明确说明资源不存在",
          "不存在" in error.get(ERROR_MESSAGE_FIELD, ""),
          f"got {error.get(ERROR_MESSAGE_FIELD)!r}")

    # Stage 18：GET /users/{id} 已收紧为「仅本人」（权限审计修正），
    # 因此这里携带 token 调用，保持原意（校验 404 + 统一错误结构）。
    response = client.get(
        f"/users/{MISSING_ID}", headers=auth_headers(owner_token)
    )
    check_error_response(
        "GET /users/{不存在} → 404（Router 显式抛出）→ 统一结构",
        response, 404, "RESOURCE_NOT_FOUND",
    )

    response = client.get("/not-a-real-endpoint")
    check_error_response(
        "未匹配路由 → 404 统一结构（框架异常也走同一 handler）",
        response, 404, "RESOURCE_NOT_FOUND",
    )



# ----------------------------------------------------------------------
# 4. 401 / 403
# ----------------------------------------------------------------------
def test_auth_errors(
    client,
    owner_token: str,
    other_token: str,
    other_id: int,
    conversation_id: int,
) -> None:
    print("\n[4] 认证 / 授权（401 / 403 语义不变）")

    response = client.get("/users/me")
    check_error_response(
        "无 token → 401 AUTHENTICATION_REQUIRED",
        response, 401, "AUTHENTICATION_REQUIRED",
        "Could not validate credentials",
    )
    check("401 仍带 WWW-Authenticate: Bearer（认证语义未变）",
          response.headers.get("www-authenticate") == "Bearer",
          f"got {response.headers.get('www-authenticate')!r}")

    response = client.get("/users/me", headers=auth_headers("not-a-valid-jwt"))
    check_error_response(
        "非法 token → 401 AUTHENTICATION_REQUIRED",
        response, 401, "AUTHENTICATION_REQUIRED",
    )

    response = client.post("/auth/login", json={
        "email": OWNER_EMAIL,
        "password": "WrongPassword123!",
    })
    check_error_response(
        "登录失败 → 401（AuthService 抛 UnauthorizedException）",
        response, 401, "AUTHENTICATION_REQUIRED", "Invalid email or password",
    )

    response = client.get(
        f"/conversations/{conversation_id}",
        headers=auth_headers(other_token),
    )
    check_error_response(
        "越权访问他人会话 → 403 PERMISSION_DENIED（Service 层抛出）",
        response, 403, "PERMISSION_DENIED",
    )

    response = client.patch(
        f"/users/{other_id}",
        json={"username": "stage10_hacked"},
        headers=auth_headers(owner_token),
    )
    check_error_response(
        "修改他人账户 → 403 PERMISSION_DENIED（Router 层抛出）",
        response, 403, "PERMISSION_DENIED",
    )


# ----------------------------------------------------------------------
# 5. Validation Error
# ----------------------------------------------------------------------
def test_validation_errors(client, owner_token: str) -> None:
    print("\n[5] Validation Error（HTTP 422 语义保持）")

    response = client.post("/users/", json={"username": "stage10_invalid"})
    error = check_error_response(
        "缺少必填字段 → 422 VALIDATION_ERROR",
        response, 422, "VALIDATION_ERROR",
    )
    check("422 message 指出失败字段，且不回显请求体原文",
          "email" in error.get(ERROR_MESSAGE_FIELD, "")
          and "stage10_invalid" not in response.text,
          f"got {error.get(ERROR_MESSAGE_FIELD)!r}")

    response = client.post(
        "/agent/chat",
        json={"conversation_id": "not-an-int", "message": "你好"},
        headers=auth_headers(owner_token),
    )
    check_error_response(
        "请求体类型错误 → 422 VALIDATION_ERROR",
        response, 422, "VALIDATION_ERROR",
    )

    response = client.get(
        "/conversations?page=0", headers=auth_headers(owner_token)
    )
    check_error_response(
        "查询参数越界 → 422 VALIDATION_ERROR",
        response, 422, "VALIDATION_ERROR",
    )

    response = client.post("/users/", json={
        "username": "stage10_invalid2",
        "email": "not-an-email",
        "password": "Secret123!",
    })
    check_error_response(
        "非法邮箱 → 422 VALIDATION_ERROR",
        response, 422, "VALIDATION_ERROR",
    )
    check("422 不泄露请求体中的密码值", "Secret123!" not in response.text)



# ----------------------------------------------------------------------
# 6. Agent Provider Error
# ----------------------------------------------------------------------
def test_provider_error(client, owner_token: str, conversation_id: int) -> None:
    print("\n[6] Agent Provider Error（Mock Provider → API 边界统一错误响应）")

    check("LLMProviderError 仍是 RuntimeError 子类（Stage 8 契约不变）",
          issubclass(LLMProviderError, RuntimeError))

    # 6.1 Runtime 层：Provider 异常仍原样向上传播（不被吞掉）
    propagated: Exception | None = None
    try:
        asyncio.run(
            AgentRuntime(ExplodingProvider()).run(
                [{"role": "user", "content": "你好"}]
            )
        )
    except LLMProviderError as exc:
        propagated = exc
    check("Runtime 原样向上传播 Provider 异常（不伪造回答）",
          isinstance(propagated, LLMProviderError),
          f"got {propagated!r}")

    # 6.2 API 边界：网络故障 → 502
    exploding = ExplodingProvider()
    app.dependency_overrides[get_agent_service] = override_agent_service(
        AgentRuntime(exploding)
    )
    try:
        response = client.post(
            "/agent/chat",
            json={"conversation_id": conversation_id, "message": "你好"},
            headers=auth_headers(owner_token),
        )
    finally:
        app.dependency_overrides.pop(get_agent_service, None)

    error = check_error_response(
        "Provider 网络故障 → 502 UPSTREAM_SERVICE_ERROR",
        response, 502, "UPSTREAM_SERVICE_ERROR",
    )
    check("502 message 为固定用户文案（不透传上游原始错误）",
          error.get(ERROR_MESSAGE_FIELD) == UPSTREAM_UNAVAILABLE_MESSAGE,
          f"got {error.get(ERROR_MESSAGE_FIELD)!r}")
    check("502 响应不含上游异常细节",
          exploding.message not in response.text
          and "Traceback" not in response.text
          and "LLMProviderError" not in response.text)
    check("Provider 故障不退化为 500（内部错误码未被占用）",
          response.status_code != 500)

    # 6.3 API 边界：上游返回非法响应（协议错误）→ 同样 502
    app.dependency_overrides[get_agent_service] = override_agent_service(
        AgentRuntime(MalformedUpstreamProvider())
    )
    try:
        response = client.post(
            "/agent/chat",
            json={"conversation_id": conversation_id, "message": "你好"},
            headers=auth_headers(owner_token),
        )
    finally:
        app.dependency_overrides.pop(get_agent_service, None)
    check_error_response(
        "上游协议错误 → 502 UPSTREAM_SERVICE_ERROR",
        response, 502, "UPSTREAM_SERVICE_ERROR",
    )
    check("502 不含上游原始响应内容",
          "bad" not in response.text and "payload" not in response.text)



# ----------------------------------------------------------------------
# 7. Tool Error Regression
# ----------------------------------------------------------------------
def test_tool_error_regression(
    client, owner_token: str, conversation_id: int
) -> None:
    print("\n[7] Tool Error Regression（Stage 8 行为不变：Tool Error ≠ HTTP 500）")

    # 7.1 三类 Tool 错误仍由 Registry 明确抛出
    registry = build_default_registry()
    cases = [
        ("UnknownToolError",
         lambda: asyncio.run(registry.execute("not_a_real_tool", {})),
         UnknownToolError),
        ("ToolParameterError",
         lambda: asyncio.run(registry.execute("calculator", {})),
         ToolParameterError),
        ("ToolExecutionError",
         lambda: asyncio.run(
             registry.execute("calculator", {"expression": "1/0"})
         ),
         ToolExecutionError),
    ]
    for label, action, expected in cases:
        raised: Exception | None = None
        try:
            action()
        except ToolError as exc:
            raised = exc
        check(f"{label} 仍由 Registry 明确抛出（Tool 层错误边界不变）",
              isinstance(raised, expected), f"got {type(raised).__name__}")

    # 7.2 Runtime 层：同一轮内 3 个 Tool 全部失败 → Tool error 回传 LLM
    script = [[
        ToolCall(id="t_unknown", name="not_a_real_tool", arguments={}),
        ToolCall(id="t_param", name="calculator", arguments={}),
        ToolCall(id="t_exec", name="calculator",
                 arguments={"expression": "1/0"}),
    ]]
    execution = asyncio.run(
        AgentRuntime(
            ScriptedToolProvider(script), tool_registry=registry
        ).execute([{"role": "user", "content": "请依次调用工具"}])
    )
    check("三个失败的 Tool 调用都被记录（tool_call ↔ tool_result 一一对应）",
          [record.name for record in execution.tool_results]
          == ["not_a_real_tool", "calculator", "calculator"],
          f"got {[r.name for r in execution.tool_results]}")
    check("Tool 失败统一以 'Tool error:' 回传给 LLM（Stage 8 格式不变）",
          execution.failed_tool_calls == 3
          and all(record.result.startswith("Tool error:")
                  for record in execution.tool_results),
          f"got {[r.result[:40] for r in execution.tool_results]}")
    check("Tool 失败不中断 Agent 执行（下一轮正常给出最终回答）",
          execution.iterations == 1
          and execution.final_response == ScriptedToolProvider.FINAL_ANSWER
          and execution.stop_reason == "final_answer",
          f"got iterations={execution.iterations} "
          f"stop_reason={execution.stop_reason!r}")

    # 7.3 HTTP 层：Tool 失败仍返回 200（绝不被 Stage 10 转成 500）
    app.dependency_overrides[get_agent_service] = override_agent_service(
        AgentRuntime(
            ScriptedToolProvider([[
                ToolCall(id="http_unknown", name="not_a_real_tool",
                         arguments={}),
                ToolCall(id="http_exec", name="calculator",
                         arguments={"expression": "1/0"}),
            ]]),
            tool_registry=build_default_registry(),
        )
    )
    try:
        response = client.post(
            "/agent/chat",
            json={"conversation_id": conversation_id, "message": "调用工具"},
            headers=auth_headers(owner_token),
        )
    finally:
        app.dependency_overrides.pop(get_agent_service, None)

    payload = json.loads(response.text) if response.text else {}
    check("Tool 失败时 /agent/chat 仍返回 200 + reply",
          response.status_code == 200
          and payload.get("reply") == ScriptedToolProvider.FINAL_ANSWER,
          f"got {response.status_code} {response.text[:160]}")
    check("Tool 失败不产生统一错误响应（响应体不含 error 字段）",
          ERROR_FIELD not in payload)

    agent_api_source = (
        APP_DIR / "api" / "agent.py"
    ).read_text(encoding="utf-8")
    check("Agent API 不捕获 ToolError（ToolError 只在 Runtime 内消化）",
          "except ToolError" not in agent_api_source
          and "app.agents.tools" not in imported_modules(agent_api_source))



# ----------------------------------------------------------------------
# 8. 未预期异常 → 500
# ----------------------------------------------------------------------
def test_unhandled_exception(client) -> None:
    print("\n[8] 未预期异常 → 500 统一结构（不泄露内部细节）")

    async def exploding_user_service():
        raise RuntimeError(
            "内部实现细节：postgresql://stage10-secret-host/should-not-leak"
        )

    app.dependency_overrides[get_user_service] = exploding_user_service
    try:
        response = client.post("/users/", json={
            "username": "stage10_boom",
            "email": "stage10_boom@example.com",
            "password": TEST_PASSWORD,
        })
    finally:
        app.dependency_overrides.pop(get_user_service, None)

    check_error_response(
        "未预期异常 → 500 INTERNAL_ERROR（固定文案）",
        response, 500, "INTERNAL_ERROR", INTERNAL_ERROR_MESSAGE,
    )
    check("500 响应不含 traceback / 异常类型 / 内部连接串",
          "Traceback" not in response.text
          and "RuntimeError" not in response.text
          and "postgresql" not in response.text
          and "should-not-leak" not in response.text)
    check("500 响应为 JSON（不是纯文本 Internal Server Error）",
          response.headers.get("content-type", "").startswith(
              "application/json"
          ),
          f"got {response.headers.get('content-type')!r}")


# ----------------------------------------------------------------------
# 9. 敏感信息保护
# ----------------------------------------------------------------------
def test_sensitive_data() -> None:
    print("\n[9] 敏感信息保护（错误响应不泄露密钥 / 内部细节）")

    markers = (
        "LLM_API_KEY",
        "SECRET_KEY",
        "DATABASE_URL",
        "Authorization",
        "Bearer ",
        "hashed_password",
        "password_hash",
        "bcrypt",
        "postgresql",
        "asyncpg",
        "site-packages",
        "Traceback",
        "BEGIN PRIVATE KEY",
        "sk-",
    )
    combined = "\n".join(error_bodies)
    leaked = [marker for marker in markers if marker in combined]
    check(
        f"错误响应体不含敏感标记（共扫描 {len(error_bodies)} 条错误响应）",
        not leaked, f"leaked={leaked}",
    )

    # 配置中的真实密钥值 / 测试密码：只做比对，不打印值本身
    value_hits: list[str] = []
    if settings.SECRET_KEY and settings.SECRET_KEY in combined:
        value_hits.append("SECRET_KEY")
    if settings.LLM_API_KEY and settings.LLM_API_KEY in combined:
        value_hits.append("LLM_API_KEY")
    if TEST_PASSWORD in combined:
        value_hits.append("TEST_PASSWORD")
    check("错误响应不含配置中的密钥值 / 测试密码（只比对，不打印）",
          not value_hits, f"leaked={value_hits}")

    check("认证失败文案保持统一（防用户名枚举，未改为回显具体原因）",
          "Invalid email or password" in combined)

    envelope_ok = True
    for body in error_bodies:
        try:
            error = json.loads(body).get(ERROR_FIELD)
        except json.JSONDecodeError:
            envelope_ok = False
            break
        if not isinstance(error, dict) or set(error) != {
            ERROR_CODE_FIELD, ERROR_MESSAGE_FIELD
        }:
            envelope_ok = False
            break
    check("全部错误响应结构一致，且无 trace_id / stack / metadata 等额外字段",
          envelope_ok)



# ----------------------------------------------------------------------
# 10. 架构边界静态检查
# ----------------------------------------------------------------------
def test_architecture_boundaries() -> None:
    print("\n[10] 架构边界静态检查（Router → Service → Runtime → Provider 未变）")

    layers = {
        "services": sorted((APP_DIR / "services").glob("*.py")),
        "agents": sorted((APP_DIR / "agents").rglob("*.py")),
        "repositories": sorted((APP_DIR / "repositories").glob("*.py")),
        "rag": sorted((APP_DIR / "rag").glob("*.py")),
    }
    web_violations: list[str] = []
    for layer, paths in layers.items():
        for path in paths:
            modules = imported_modules(path.read_text(encoding="utf-8"))
            hits = [
                module for module in modules
                if module.split(".")[0] in {"fastapi", "starlette"}
            ]
            if hits:
                web_violations.append(f"{layer}/{path.name}: {sorted(hits)}")
    check("Service / Runtime / Provider / Tool / Repository / RAG 均不依赖 FastAPI",
          not web_violations, f"{web_violations}")

    repo_source = "".join(
        path.read_text(encoding="utf-8")
        for path in (APP_DIR / "repositories").glob("*.py")
    )
    check("Repository 不负责 HTTP（无 HTTPException / JSONResponse / status_code）",
          "HTTPException" not in repo_source
          and "JSONResponse" not in repo_source
          and "status_code" not in repo_source)

    runtime_source = (
        APP_DIR / "agents" / "runtime.py"
    ).read_text(encoding="utf-8")
    check("Runtime 依赖集合未变（Stage 8 / 9 契约）",
          imported_modules(runtime_source) == {
              "app.agents.execution",
              "app.agents.observers",
              "app.agents.providers",
              "app.agents.providers.base",
              "app.agents.tools.base",
              "app.agents.tools.registry",
          },
          f"got {sorted(imported_modules(runtime_source))}")
    check("Runtime 仍只把 ToolError 转成 tool_result（无异常 → HTTP 逻辑）",
          "except ToolError" in runtime_source
          and "HTTPException" not in runtime_source)

    service_source = (
        APP_DIR / "services" / "agent.py"
    ).read_text(encoding="utf-8")
    # Stage 17：Service 按 Agent 配置选择 Runtime，调用契约不变
    check("Service 仍以 runtime.run(messages, system_prompt=...) 调用（契约未变）",
          "runtime.run(messages, system_prompt=system_prompt)"
          in service_source)
    check("Service 不依赖 FastAPI（无 HTTPException / JSONResponse）",
          "HTTPException" not in service_source
          and "JSONResponse" not in service_source)

    agent_api_source = (
        APP_DIR / "api" / "agent.py"
    ).read_text(encoding="utf-8")
    api_imports = imported_modules(agent_api_source)
    check("Agent Router 仍只走 Service（不导入 Repository / Session / 具体 Provider）",
          "app.repositories" not in api_imports
          and "app.database.session" not in api_imports
          and "app.agents.providers.qwen" not in api_imports
          and "app.agents.providers.ollama" not in api_imports,
          f"got {sorted(api_imports)}")
    check("Agent Router 只在 API 边界转换 Provider 错误（→ 502）",
          "LLMProviderError" in agent_api_source
          and "UpstreamServiceException" in agent_api_source)

    main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    check("main.py 通过 register_exception_handlers 统一注册（单一错误边界）",
          "register_exception_handlers(app)" in main_source)

    app_files = sorted(APP_DIR.rglob("*.py"))
    callers = sorted(
        path.name for path in app_files
        if "add_exception_handler" in path.read_text(encoding="utf-8")
    )
    check("add_exception_handler 只出现在 exception_handlers.py（不散落各处）",
          callers == ["exception_handlers.py"], f"got {callers}")

    handlers_source = (
        APP_DIR / "core" / "exception_handlers.py"
    ).read_text(encoding="utf-8")
    check("未引入分布式追踪 / 事件总线 / 错误上报框架 / 额外中间件",
          not any(
              term in handlers_source
              for term in ("opentelemetry", "sentry", "kafka", "celery",
                           "redis", "EventBus", "trace_id", "request_id")
          ))
    check("错误响应结构唯一来源为 build_error_payload（无重复拼装）",
          handlers_source.count("build_error_payload(") >= 4)


# ----------------------------------------------------------------------
# 测试数据准备 / 清理
# ----------------------------------------------------------------------
def register_and_login(
    client, username: str, email: str
) -> tuple[str | None, int | None]:
    """注册（或复用已存在）测试用户并登录，返回 (token, user_id)。"""
    response = client.post("/users/", json={
        "username": username,
        "email": email,
        "password": TEST_PASSWORD,
    })
    if response.status_code not in (201, 409):
        print(f"  创建测试用户失败: {response.status_code} {response.text[:160]}")
        return None, None

    login = client.post("/auth/login", json={
        "email": email,
        "password": TEST_PASSWORD,
    })
    if login.status_code != 200:
        print(f"  登录失败: {login.status_code} {login.text[:160]}")
        return None, None

    token = login.json().get("access_token") or ""
    if not token:
        return None, None

    me = client.get("/users/me", headers=auth_headers(token))
    user_id = me.json().get("id") if me.status_code == 200 else None
    return token, user_id


def create_conversation(client, token: str) -> int | None:
    """创建测试会话，返回会话 ID。"""
    response = client.post(
        "/conversations",
        json={"title": "Stage 10 异常规范验收会话"},
        headers=auth_headers(token),
    )
    if response.status_code != 201:
        print(f"  创建会话失败: {response.status_code} {response.text[:160]}")
        return None
    return response.json().get("id")


def cleanup(
    client,
    owner_token: str | None,
    owner_id: int | None,
    other_token: str | None,
    other_id: int | None,
    conversation_id: int | None,
) -> None:
    """删除测试会话与测试用户（不留垃圾数据）。"""
    print("\n[11] 清理测试数据")

    if conversation_id and owner_token:
        response = client.delete(
            f"/conversations/{conversation_id}",
            headers=auth_headers(owner_token),
        )
        check("删除测试会话", response.status_code == 204,
              f"got {response.status_code}")

    if other_id and other_token:
        response = client.delete(
            f"/users/{other_id}", headers=auth_headers(other_token)
        )
        check("删除测试用户（other）", response.status_code == 204,
              f"got {response.status_code}")

    if owner_id and owner_token:
        response = client.delete(
            f"/users/{owner_id}", headers=auth_headers(owner_token)
        )
        check("删除测试用户（owner）", response.status_code == 204,
              f"got {response.status_code}")

    if owner_token:
        response = client.get("/users/me", headers=auth_headers(owner_token))
        check("清理后原 token 失效（401 语义不变）",
              response.status_code == 401, f"got {response.status_code}")


def main() -> None:
    # 进程内运行真实 app；raise_server_exceptions=False 让 500 兜底处理器
    # 返回的响应可以被断言（Starlette 仍会把异常写入服务端日志）
    with TestClient(app, raise_server_exceptions=False) as client:
        print("\n[0] 测试数据准备")
        owner_token, owner_id = register_and_login(
            client, OWNER_USERNAME, OWNER_EMAIL
        )
        other_token, other_id = register_and_login(
            client, OTHER_USERNAME, OTHER_EMAIL
        )
        if not owner_token or not other_token or not owner_id or not other_id:
            print("无法准备测试用户，测试中止")
            sys.exit(1)

        conversation_id = create_conversation(client, owner_token)
        if not conversation_id:
            print("无法创建测试会话，测试中止")
            sys.exit(1)

        try:
            test_exception_types()
            test_exception_handlers(client, owner_token)
            test_not_found(client, owner_token)
            test_auth_errors(
                client, owner_token, other_token, other_id, conversation_id,
            )
            test_validation_errors(client, owner_token)
            test_provider_error(client, owner_token, conversation_id)
            test_tool_error_regression(client, owner_token, conversation_id)
            test_unhandled_exception(client)
            test_sensitive_data()
            test_architecture_boundaries()
        finally:
            cleanup(
                client, owner_token, owner_id, other_token, other_id,
                conversation_id,
            )
            # 依赖覆盖必须清空，避免影响同进程内的其他测试
            app.dependency_overrides.clear()

    print("\n" + "=" * 60)
    print("Stage 10 Exception Handling acceptance: "
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
