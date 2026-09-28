"""Stage 6：Tool Calling 架构验收测试（进程内，不依赖数据库 / 服务器）。

覆盖：
1. Calculator 正确性
2. Calculator 安全性（禁止任意 Python 代码）
3. Tool Registry（register / get / list_tools / unknown tool）
4. Tool Schema 完整性
5. Tool Calling 全流程（Mock Provider）+ Tool Result 回传 + 错误回传
6. 无 Tool 时的普通对话（回归）
7. 未实现原生 Tool Calling 的 Provider 兼容性
8. Provider 响应解析（原生 tool_calls 格式）
9. Tool 异常体系

本脚本保持 Stage 6 的原始覆盖范围。Stage 7 新增的
「多 Tool + 参数校验」覆盖见 ``scripts/tool_validation_acceptance_test.py``。

本脚本不读取 / 打印 backend/.env 中的任何敏感值。
"""

import ast
import asyncio
import sys
from pathlib import Path

from app.agents.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCall,
    parse_chat_completion,
)
from app.agents.runtime import AgentRuntime
from app.agents.tools import (
    CalculatorTool,
    Tool,
    ToolError,
    ToolExecutionError,
    ToolRegistry,
    UnknownToolError,
    build_default_registry,
)

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


# ----------------------------------------------------------------------
# 1. Calculator 正确性
# ----------------------------------------------------------------------
def test_calculator() -> None:
    print("\n[1] Calculator 正确性")
    tool = CalculatorTool()

    cases = [
        ("2 + 3", "5"),
        ("2 + 3 * 4", "14"),
        ("(10 - 2) / 4", "2"),
        ("100 / 8", "12.5"),
        ("-5 + 3", "-2"),
        ("2 * (3 + 4) - 1", "13"),
    ]
    for expression, expected in cases:
        result = asyncio.run(tool.execute({"expression": expression}))
        check(
            f"calculator({expression!r}) == {expected}",
            result == expected,
            f"got {result!r}",
        )

    # 参数错误
    for bad_args, label in (
        ({}, "缺少 expression"),
        ({"expression": 123}, "expression 非字符串"),
        ({"expression": "   "}, "expression 为空"),
    ):
        raised = False
        try:
            asyncio.run(tool.execute(bad_args))
        except ToolExecutionError:
            raised = True
        check(f"参数错误明确报错（{label}）", raised)


# ----------------------------------------------------------------------
# 2. Calculator 安全性
# ----------------------------------------------------------------------
def test_calculator_security() -> None:
    print("\n[2] Calculator 安全性")
    tool = CalculatorTool()

    dangerous = [
        '__import__("os").system("echo hacked")',
        "__import__('os')",
        'open("/etc/passwd").read()',
        'open("secret.txt")',
        'exec("print(1)")',
        'eval("1 + 1")',
        "1 + a",
        "().__class__.__bases__",
        "[1, 2][0]",
        "lambda: 1",
        "print(1)",
        '"a" * 3',
        "2 ** 10",
        "5 % 2",
        "1 // 2",
        "x = 1",
        "f(1)",
        "__builtins__",
    ]
    for expression in dangerous:
        blocked = False
        try:
            result = asyncio.run(tool.execute({"expression": expression}))
            print(f"      [WARN] 未拦截: {expression!r} -> {result!r}")
        except ToolExecutionError:
            blocked = True
        check(f"已拦截危险表达式: {expression[:32]!r}", blocked)

    # 除零
    raised = False
    try:
        asyncio.run(tool.execute({"expression": "1 / 0"}))
    except ToolExecutionError:
        raised = True
    check("除零明确报错", raised)

    # 确认未调用 eval/exec（用 AST 精确判断，避免误伤 safe_eval 之类的名字）
    builtin_path = (
        Path(__file__).resolve().parent.parent
        / "app" / "agents" / "tools" / "builtin.py"
    )
    source = builtin_path.read_text(encoding="utf-8")
    called_names = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    check("builtin.py 未调用 eval()", "eval" not in called_names,
          f"called={sorted(called_names)}")
    check("builtin.py 未调用 exec()", "exec" not in called_names,
          f"called={sorted(called_names)}")
    check("builtin.py 使用 ast 白名单解析", "ast.parse" in source)


# ----------------------------------------------------------------------
# 3. Tool Registry
# ----------------------------------------------------------------------
class DummyTool(Tool):
    """测试用简单工具。"""

    name = "dummy"
    description = "测试用工具"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, arguments: dict) -> str:
        return f"dummy:{arguments.get('text', '')}"


def test_registry() -> None:
    print("\n[3] Tool Registry")

    registry = ToolRegistry()
    check("初始为空", len(registry) == 0)

    registry.register(CalculatorTool())
    registry.register(DummyTool())
    check("register 后数量为 2", len(registry) == 2)

    tool = registry.get("calculator")
    check("get('calculator') 返回 CalculatorTool",
          isinstance(tool, CalculatorTool))

    names = [t.name for t in registry.list_tools()]
    check("list_tools 返回全部工具（稳定排序）",
          names == ["calculator", "dummy"], f"got {names}")

    # unknown tool
    raised = False
    try:
        registry.get("not_exist")
    except UnknownToolError as exc:
        raised = True
        check("UnknownToolError 消息含可用工具",
              "calculator" in str(exc), f"msg={exc}")
    check("unknown tool 明确报错", raised)

    # 重复注册
    dup = False
    try:
        registry.register(CalculatorTool())
    except ValueError:
        dup = True
    check("重复注册明确报错（不静默覆盖）", dup)

    # execute 转发
    result = asyncio.run(
        registry.execute("calculator", {"expression": "6 * 7"})
    )
    check("registry.execute 转发到 Tool", result == "42", f"got {result}")

    result = asyncio.run(registry.execute("dummy", {"text": "hi"}))
    check("registry.execute 多工具可用", result == "dummy:hi", f"got {result}")


# ----------------------------------------------------------------------
# 4. Tool Schema
# ----------------------------------------------------------------------
def test_schema() -> None:
    print("\n[4] Tool Schema")
    schema = CalculatorTool().get_schema()

    check("schema.type == 'function'", schema.get("type") == "function")
    function = schema.get("function", {})
    check("schema 含 name", function.get("name") == "calculator")
    check("schema 含 description", bool(function.get("description")))

    parameters = function.get("parameters", {})
    check("parameters.type == 'object'", parameters.get("type") == "object")
    check("parameters.properties.expression 存在",
          "expression" in parameters.get("properties", {}))
    check("parameters.required 含 expression",
          parameters.get("required") == ["expression"])

    schemas = build_default_registry().get_schemas()
    check("registry.get_schemas 返回全部内置 Schema", len(schemas) == 2,
          f"got {len(schemas)}")
    names = [schema["function"]["name"] for schema in schemas]
    check("Schema 名称含全部内置 Tool（calculator / datetime）",
          names == ["calculator", "datetime"], f"got {names}")


# ----------------------------------------------------------------------
# 5. Tool Calling 全流程（Mock Provider）
# ----------------------------------------------------------------------
class RecordingProvider(LLMProvider):
    """按脚本返回 tool_call 的假 Provider，并记录每轮 messages/tools。"""

    def __init__(
        self,
        tool_calls: list[ToolCall] | None = None,
        final_answer: str = "FINAL",
    ) -> None:
        self.scripted_tool_calls = list(tool_calls or [])
        self.final_answer = final_answer
        self.generate_calls: list[list[dict]] = []
        self.messages_seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    async def generate(self, messages: list[dict]) -> str:
        self.generate_calls.append(messages)
        return self.final_answer

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.messages_seen.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        if self.scripted_tool_calls:
            call = self.scripted_tool_calls.pop(0)
            return LLMResponse(content=None, tool_calls=[call])
        return LLMResponse(content=self.final_answer)


def test_tool_calling_flow() -> None:
    print("\n[5] Tool Calling 全流程（Mock Provider）")

    provider = RecordingProvider(
        tool_calls=[
            ToolCall(
                id="call_abc",
                name="calculator",
                arguments={"expression": "25 * 4 + 10"},
            )
        ],
        final_answer="计算结果是 110。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())

    reply = asyncio.run(
        runtime.run([{"role": "user", "content": "计算 25 * 4 + 10"}])
    )

    check("最终回答来自 LLM 第二轮", reply == "计算结果是 110。",
          f"got {reply!r}")
    check("恰好两轮 LLM 调用（tool_call → 最终回答）",
          len(provider.messages_seen) == 2,
          f"got {len(provider.messages_seen)}")
    check("LLM 收到 calculator 的 Tool Schema",
          bool(provider.tools_seen)
          and provider.tools_seen[0][0]["function"]["name"] == "calculator")

    second_round = provider.messages_seen[1]
    assistant_msgs = [
        m for m in second_round
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    tool_msgs = [m for m in second_round if m.get("role") == "tool"]

    check("第二轮包含 assistant.tool_calls 消息", bool(assistant_msgs))
    check("assistant.tool_calls 为 OpenAI 格式",
          bool(assistant_msgs)
          and assistant_msgs[0]["tool_calls"][0]["function"]["name"]
          == "calculator")
    check("第二轮包含 tool 结果消息", bool(tool_msgs))
    check("tool_result 内容为 110",
          bool(tool_msgs) and tool_msgs[0]["content"] == "110",
          f"got {tool_msgs[0]['content'] if tool_msgs else None!r}")
    check("tool_call_id 与请求一致",
          bool(tool_msgs) and tool_msgs[0]["tool_call_id"] == "call_abc")
    check("system prompt 仍在首位",
          second_round[0].get("role") == "system")


def test_tool_error_feedback() -> None:
    print("\n[5b] Tool 错误明确回传（不静默吞掉）")

    # 未知工具
    p1 = RecordingProvider(
        tool_calls=[ToolCall(id="c1", name="not_exist_tool", arguments={})],
        final_answer="已处理错误",
    )
    reply = asyncio.run(
        AgentRuntime(p1, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "调用不存在的工具"}]
        )
    )
    tool_msgs = [m for m in p1.messages_seen[1] if m.get("role") == "tool"]
    check("未知工具不中断对话", reply == "已处理错误", f"got {reply!r}")
    check("未知工具错误含 'Unknown tool'",
          bool(tool_msgs) and "Unknown tool" in tool_msgs[0]["content"],
          f"got {tool_msgs[0]['content'] if tool_msgs else None!r}")

    # 参数缺失
    p2 = RecordingProvider(
        tool_calls=[ToolCall(id="c2", name="calculator", arguments={})],
        final_answer="done",
    )
    asyncio.run(
        AgentRuntime(p2, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "算"}]
        )
    )
    tool_msgs2 = [m for m in p2.messages_seen[1] if m.get("role") == "tool"]
    check("参数缺失错误含 'expression'",
          bool(tool_msgs2) and "expression" in tool_msgs2[0]["content"],
          f"got {tool_msgs2[0]['content'] if tool_msgs2 else None!r}")

    # 危险表达式
    p3 = RecordingProvider(
        tool_calls=[
            ToolCall(
                id="c3",
                name="calculator",
                arguments={"expression": '__import__("os")'},
            )
        ],
        final_answer="done",
    )
    asyncio.run(
        AgentRuntime(p3, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "执行代码"}]
        )
    )
    tool_msgs3 = [m for m in p3.messages_seen[1] if m.get("role") == "tool"]
    check("危险表达式被拒绝且以错误回传",
          bool(tool_msgs3) and "Tool error" in tool_msgs3[0]["content"],
          f"got {tool_msgs3[0]['content'] if tool_msgs3 else None!r}")


def test_multiple_tool_calls() -> None:
    print("\n[5c] 单轮多个 tool_calls")

    class MultiCallProvider(LLMProvider):
        """第一轮一次返回两个 tool_calls。"""

        def __init__(self) -> None:
            self.rounds = 0
            self.messages_seen: list[list[dict]] = []

        async def generate(self, messages: list[dict]) -> str:
            return "完成"

        async def generate_with_tools(self, messages, tools):
            self.rounds += 1
            self.messages_seen.append([dict(m) for m in messages])
            if self.rounds == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="m1", name="calculator",
                                 arguments={"expression": "1 + 1"}),
                        ToolCall(id="m2", name="calculator",
                                 arguments={"expression": "2 * 3"}),
                    ],
                )
            return LLMResponse(content="完成")

    provider = MultiCallProvider()
    reply = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "算两个式子"}]
        )
    )
    check("两个 tool_call 同轮执行", reply == "完成", f"got {reply!r}")

    tool_msgs = [
        m for m in provider.messages_seen[1] if m.get("role") == "tool"
    ]
    check("产生两条 tool 结果", len(tool_msgs) == 2, f"got {len(tool_msgs)}")
    check("结果分别为 2 与 6",
          [m["content"] for m in tool_msgs] == ["2", "6"],
          f"got {[m['content'] for m in tool_msgs]}")
    check("两条 tool 结果的 tool_call_id 不同",
          len({m["tool_call_id"] for m in tool_msgs}) == 2,
          f"got {[m['tool_call_id'] for m in tool_msgs]}")


def test_max_iterations_guard() -> None:
    print("\n[5d] 最大轮数保护（防无限循环）")

    class AlwaysToolProvider(LLMProvider):
        def __init__(self) -> None:
            self.tool_rounds = 0

        async def generate(self, messages: list[dict]) -> str:
            return "最终兜底回答"

        async def generate_with_tools(self, messages, tools):
            self.tool_rounds += 1
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"loop_{self.tool_rounds}",
                        name="calculator",
                        arguments={"expression": "1 + 1"},
                    )
                ],
            )

    provider = AlwaysToolProvider()
    reply = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "一直调用工具"}]
        )
    )
    check("不会无限循环（受 MAX_TOOL_ITERATIONS 限制）",
          provider.tool_rounds <= 3, f"got {provider.tool_rounds}")
    check("超限后返回兜底回答", reply == "最终兜底回答", f"got {reply!r}")


# ----------------------------------------------------------------------
# 6. 无 Tool 时普通对话（回归）
# ----------------------------------------------------------------------
def test_no_tool_regression() -> None:
    print("\n[6] 无 Tool 普通对话（回归）")

    class PlainProvider(LLMProvider):
        def __init__(self) -> None:
            self.seen: list[dict] = []

        async def generate(self, messages: list[dict]) -> str:
            self.seen = messages
            return "普通回答"

    provider = PlainProvider()
    runtime = AgentRuntime(provider, tool_registry=None)
    reply = asyncio.run(runtime.run([{"role": "user", "content": "你好"}]))

    check("tool_registry=None → 普通文本回答", reply == "普通回答",
          f"got {reply!r}")
    check("普通对话仍注入 system prompt",
          provider.seen[0]["role"] == "system")

    provider2 = PlainProvider()
    runtime2 = AgentRuntime(provider2, tool_registry=ToolRegistry())
    reply2 = asyncio.run(runtime2.run([{"role": "user", "content": "你好"}]))
    check("空 Registry → 普通文本回答", reply2 == "普通回答",
          f"got {reply2!r}")


# ----------------------------------------------------------------------
# 7. 未实现原生 Tool Calling 的 Provider 兼容性
# ----------------------------------------------------------------------
def test_provider_without_native_tools() -> None:
    print("\n[7] 未覆写 generate_with_tools 的 Provider 兼容性")

    class LegacyProvider(LLMProvider):
        """只实现 generate（模拟不支持原生 Tool Calling 的 Provider）。"""

        async def generate(self, messages: list[dict]) -> str:
            return "legacy answer"

    provider = LegacyProvider()
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    reply = asyncio.run(runtime.run([{"role": "user", "content": "你好"}]))

    check("默认 generate_with_tools 退化为普通生成（零回归）",
          reply == "legacy answer", f"got {reply!r}")

    response = asyncio.run(
        provider.generate_with_tools([{"role": "user", "content": "hi"}], [])
    )
    check("默认实现返回 LLMResponse", isinstance(response, LLMResponse))
    check("默认实现不含 tool_calls", not response.has_tool_calls)


# ----------------------------------------------------------------------
# 8. Provider 原生响应解析
# ----------------------------------------------------------------------
def test_parse_chat_completion() -> None:
    print("\n[8] Provider 响应解析（原生 tool_calls 格式）")

    plain = {"choices": [{"message": {"role": "assistant", "content": "你好"}}]}
    response = parse_chat_completion(plain, provider_label="Test")
    check("解析纯文本响应",
          response.content == "你好" and not response.has_tool_calls)

    with_tool = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression": "2 + 3"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    parsed = parse_chat_completion(with_tool, provider_label="Test")
    check("解析 tool_calls 响应", parsed.has_tool_calls)
    check("解析 tool_call name", parsed.tool_calls[0].name == "calculator")
    check("解析 tool_call arguments 为 dict",
          parsed.tool_calls[0].arguments == {"expression": "2 + 3"},
          f"got {parsed.tool_calls[0].arguments!r}")
    check("ToolCall.to_openai_dict 可回传",
          parsed.tool_calls[0].to_openai_dict()["function"]["name"]
          == "calculator")

    raised = False
    try:
        parse_chat_completion({"bad": "data"}, provider_label="Test")
    except RuntimeError:
        raised = True
    check("异常响应明确报错", raised)

    raised = False
    try:
        parse_chat_completion(
            {
                "choices": [
                    {"message": {"tool_calls": [
                        {"function": {"name": "x", "arguments": "not-json"}}
                    ]}}
                ]
            },
            provider_label="Test",
        )
    except RuntimeError:
        raised = True
    check("非法 arguments JSON 明确报错", raised)


# ----------------------------------------------------------------------
# 9. Tool 异常体系
# ----------------------------------------------------------------------
def test_error_hierarchy() -> None:
    print("\n[9] Tool 异常体系")
    check("UnknownToolError 继承 ToolError",
          issubclass(UnknownToolError, ToolError))
    check("ToolExecutionError 继承 ToolError",
          issubclass(ToolExecutionError, ToolError))
    check("ToolError 是 Exception 子类",
          issubclass(ToolError, Exception))


def main() -> None:
    test_calculator()
    test_calculator_security()
    test_registry()
    test_schema()
    test_tool_calling_flow()
    test_tool_error_feedback()
    test_multiple_tool_calls()
    test_max_iterations_guard()
    test_no_tool_regression()
    test_provider_without_native_tools()
    test_parse_chat_completion()
    test_error_hierarchy()

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

