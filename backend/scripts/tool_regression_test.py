"""Stage 6：Agent Chat / RAG / Provider 回归测试（HTTP 端到端）。

覆盖：
- 普通聊天 / 多轮聊天（Conversation History 持久化）
- RAG Context 注入路径
- Conversation CRUD
- Authentication 回归
- Provider 工厂链路（通过 /agent/chat 实际走通）

测试结束后 **清理测试数据**（删除会话与测试用户），不留垃圾数据。
本脚本不读取 / 打印 backend/.env 中的任何敏感值。
"""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8001"

TEST_USERNAME = "tool_regression_user"
TEST_EMAIL = f"{TEST_USERNAME}@example.com"
TEST_PASSWORD = "Test12345!"

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def http_request(
    method: str,
    path: str,
    data: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def main() -> None:
    # ---------------- Authentication（回归） ----------------
    print("\n[1] Authentication 回归")
    status, _ = http_request("GET", "/users/me")
    check("无 token 访问 /users/me → 401", status == 401, f"got {status}")

    status, body = http_request("POST", "/users/", {
        "username": TEST_USERNAME,
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD,
    })
    if status not in (201, 409):
        print(f"  创建测试用户失败: {status} {body}")
        sys.exit(1)
    print(f"  创建/已存在测试用户: status={status}")

    status, body = http_request("POST", "/auth/login", {
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD,
    })
    check("登录获取 token", status == 200 and "access_token" in body,
          f"got {status}")
    token = body.get("access_token", "")
    if not token:
        print("无法获取 token，测试中止")
        sys.exit(1)

    status, body = http_request("GET", "/users/me", token=token)
    check("认证后 /users/me 正常",
          status == 200 and body.get("username") == TEST_USERNAME,
          f"got {status}")
    user_id = body.get("id")

    conversation_id = None
    try:
        # ---------------- Conversation CRUD ----------------
        print("\n[2] Conversation CRUD 回归")
        status, body = http_request(
            "POST", "/conversations", {"title": "Tool 回归会话"}, token=token
        )
        check("创建会话", status == 201 and body.get("id"), f"got {status}")
        conversation_id = body.get("id")

        status, body = http_request("GET", "/conversations", token=token)
        check("列出会话", status == 200 and "items" in body, f"got {status}")

        status, body = http_request(
            "GET", f"/conversations/{conversation_id}", token=token
        )
        check("获取会话详情",
              status == 200 and body.get("id") == conversation_id,
              f"got {status}")

        status, body = http_request(
            "PATCH", f"/conversations/{conversation_id}",
            {"title": "Tool 回归会话-已改名"}, token=token,
        )
        check("重命名会话",
              status == 200 and body.get("title") == "Tool 回归会话-已改名",
              f"got {status}")

        # ---------------- Agent Chat（普通） ----------------
        print("\n[3] Agent Chat 普通对话")
        status, body = http_request("POST", "/agent/chat", {
            "conversation_id": conversation_id,
            "message": "你好，请介绍一下你自己",
        }, token=token)
        check("普通聊天返回 200 + reply",
              status == 200 and body.get("reply"), f"got {status} {body}")
        print(f"  reply: {str(body.get('reply'))[:100]}")

        # ---------------- 多轮对话 ----------------
        print("\n[4] 多轮对话（History 持久化）")
        status, body = http_request("POST", "/agent/chat", {
            "conversation_id": conversation_id,
            "message": "我刚才说了什么？",
        }, token=token)
        check("第二轮对话正常", status == 200 and body.get("reply"),
              f"got {status}")

        # ---------------- RAG 路径 ----------------
        print("\n[5] RAG Context 注入路径")
        status, body = http_request("POST", "/agent/chat", {
            "conversation_id": conversation_id,
            "message": "北京有哪些适合旅游的景点？",
        }, token=token)
        check("RAG 查询正常返回", status == 200 and body.get("reply"),
              f"got {status}")

        # ---------------- 计算类输入（Tool 已启用） ----------------
        print("\n[6] 计算类输入（Tool 已启用，不破坏对话）")
        status, body = http_request("POST", "/agent/chat", {
            "conversation_id": conversation_id,
            "message": "计算 25 * 4 + 10",
        }, token=token)
        check("计算类输入正常返回", status == 200 and body.get("reply"),
              f"got {status}")
        print(f"  reply: {str(body.get('reply'))[:100]}")

    finally:
        # ---------------- 清理测试数据 ----------------
        print("\n[7] 清理测试数据")
        if conversation_id:
            status, _ = http_request(
                "DELETE", f"/conversations/{conversation_id}", token=token
            )
            check("删除测试会话", status == 204, f"got {status}")

        if user_id:
            status, _ = http_request(
                "DELETE", f"/users/{user_id}", token=token
            )
            check("删除测试用户", status == 204, f"got {status}")

        status, _ = http_request("GET", "/users/me", token=token)
        check("清理后原 token 失效（用户已删除）", status == 401,
              f"got {status}")

    print("\n" + "=" * 56)
    print(f"通过: {len(passed)}  失败: {len(failed)}")
    if failed:
        print("失败项:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通过！")


if __name__ == "__main__":
    main()
