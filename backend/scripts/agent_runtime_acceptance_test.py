"""Stage 8：Agent Execution Loop 验收测试（进程内，不依赖数据库 / 服务器）。

覆盖 Stage 8 目标（Agent 一次执行过程的状态与边界）：

1. 普通文本回答（无 tool_calls，不进入 Tool 循环）
2. 单 Tool Calling → Tool Result → Final Answer
3. 连续两轮 Tool Calling（两个不同 Tool）
4. 同一轮多个 Tool Calls
5. Unknown Tool（UnknownToolError）
6. 参数校验失败（ToolParameterError）
7. Tool execution error（ToolExecutionError / 非预期异常 / 返回值异常）
8. 最大 iteration 限制（默认上限 + 自定义上限 + 非法上限）
9. Tool Calling 后最终回答（含达到上限时的兜底回答）
10. 原始 messages 没有被错误污染
11. tool_call_id / tool name / arguments 正确传递
12. Provider 错误边界（正常 / tool_calls / 空响应 / malformed / 异常传播）
13. 执行状态（AgentExecution / ToolExecution）字段语义
14. 架构边界静态检查（Runtime 不依赖具体 Tool / FastAPI / DB / RAG；Service 契约不变）

与既有测试的关系
----------------
Stage 6（``tool_acceptance_test.py``）与 Stage 7
（``tool_validation_acceptance_test.py``）已经覆盖了「Tool 是否可用 / 参数校验」
等 Tool 层能力；本脚本不重复验证 Tool 自身逻辑，而是聚焦 **Runtime 执行循环**
本身：迭代次数、结束原因、消息完整性、状态记录与错误边界。
Calculator / DateTime 的具体数值正确性仍由 Stage 6 / 7 覆盖。

运行方式（在 backend/ 目录下）::

    python -m scripts.agent_runtime_acceptance_test

本脚本不读取 / 打印 backend/.env 中的任何敏感值。
"""

import ast
import asyncio
import copy
import dataclasses
import json
import sys
from pathlib import Path

from app.agents.execution import (
    STOP_REASON_FINAL_ANSWER,
    STOP_REASON_MAX_ITERATIONS,
    TOOL_ERROR_PREFIX,
    AgentExecution,
    ToolExecution,
)
from app.agents.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCall,
    parse_chat_completion,
)
from app.agents.runtime import (
    EMPTY_RESPONSE_FALLBACK,
    MAX_TOOL_ITERATIONS,
    AgentRuntime,
)
from app.agents.tools import (
    Tool,
    ToolError,
    ToolExecutionError,
    ToolParameterError,
    ToolRegistry,
    UnknownToolError,
    build_default_registry,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BACKEND_DIR / "app" / "agents"

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
# 测试用 Provider（只实现 LLMProvider 抽象，不接触真实网络）
# ----------------------------------------------------------------------
class ScriptedProvider(LLMProvider):
    """按脚本依次返回 tool_calls；脚本用完后返回最终回答。

    记录每一轮 ``generate_with_tools`` 收到的 messages / tools，
    以及 ``generate``（兜底路径）被调用的次数。
    """

    def __init__(
        self,
        script: list[ToolCall] | None = None,
        final_answer: str = "FINAL",
    ) -> None:
        self.script = list(script or [])
        self.final_answer = final_answer
        self.messages_seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []
        self.generate_calls = 0

    async def generate(self, messages: list[dict]) -> str:
        self.generate_calls += 1
        return self.final_answer

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.messages_seen.append(list(messages))
        self.tools_seen.append(tools)
        if self.script:
            return LLMResponse(content=None, tool_calls=[self.script.pop(0)])
        return LLMResponse(content=self.final_answer)


class NoneContentProvider(LLMProvider):
    """始终返回空响应（``content=None`` 且无 tool_calls）的 Provider。"""

    def __init__(self) -> None:
        self.with_tools_calls = 0
        self.generate_calls = 0

    async def generate(self, messages: list[dict]) -> str | None:  # type: ignore[override]
        self.generate_calls += 1
        return None

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.with_tools_calls += 1
        return LLMResponse(content=None)


class AlwaysToolProvider(LLMProvider):
    """每轮都要求调用 Tool（永不给出最终回答）的 Provider。"""

    def __init__(self, tool_name: str = "calculator") -> None:
        self.tool_name = tool_name
        self.tool_rounds = 0
        self.generate_calls = 0
        self.messages_seen: list[list[dict]] = []

    async def generate(self, messages: list[dict]) -> str:
        self.generate_calls += 1
        self.messages_seen.append(list(messages))
        return "兜底最终回答"

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.tool_rounds += 1
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"loop_{self.tool_rounds}",
                    name=self.tool_name,
                    arguments={"expression": "1 + 1"},
                )
            ],
        )


class ExplodingProvider(LLMProvider):
    """``generate_with_tools`` 抛异常（模拟 network / provider 故障）。"""

    async def generate(self, messages: list[dict]) -> str:
        return "不应到达这里"

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        raise RuntimeError("provider 网络故障")


class ExplodingFallbackProvider(AlwaysToolProvider):
    """每轮都要求调用 Tool，且兜底 ``generate`` 也失败。"""

    async def generate(self, messages: list[dict]) -> str:
        raise RuntimeError("provider 兜底调用失败")


# ----------------------------------------------------------------------
# 测试用 Tool（不进入 build_default_registry）
# ----------------------------------------------------------------------
class SpyTool(Tool):
    """记录每次 execute 的参数（含必填 integer 参数）。"""

    name = "spy"
    description = "测试用工具（记录调用）"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, arguments: dict) -> str:
        self.calls.append(dict(arguments))
        return f"spy:{arguments['value']}"


class BoomTool(Tool):
    """总是抛出 ToolExecutionError（工具业务失败）。"""

    name = "boom"
    description = "测试用工具（总是失败）"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict) -> str:
        raise ToolExecutionError("boom 工具执行失败")


class CrashTool(Tool):
    """抛出非 ToolError 异常（模拟 Tool 实现自身的 bug）。"""

    name = "crash"
    description = "测试用工具（抛出非预期异常）"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict) -> str:
        raise ValueError("工具内部 bug")


class NoneResultTool(Tool):
    """返回 None（非法返回值）。"""

    name = "none_result"
    description = "测试用工具（返回 None）"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict) -> str:
        return None  # type: ignore[return-value]


class IntResultTool(Tool):
    """返回 int（需要 Runtime 归一化为字符串）。"""

    name = "int_result"
    description = "测试用工具（返回 int）"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict) -> str:
        return 42  # type: ignore[return-value]


class CancelTool(Tool):
    """抛出 asyncio.CancelledError（模拟任务取消）。"""

    name = "cancel"
    description = "测试用工具（模拟任务取消）"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict) -> str:
        raise asyncio.CancelledError()


def build_probe_registry() -> ToolRegistry:
    """构建仅含测试用 Tool 的注册表（不污染内置 Tool 注册点）。"""
    return ToolRegistry(
        tools=[
            SpyTool(),
            BoomTool(),
            CrashTool(),
            NoneResultTool(),
            IntResultTool(),
            CancelTool(),
        ]
    )


# ----------------------------------------------------------------------
# 断言辅助
# ----------------------------------------------------------------------
def round_tool_messages(provider: ScriptedProvider, index: int) -> list[dict]:
    """第 N 轮发给 LLM 的 tool 结果消息。"""
    return [
        message
        for message in provider.messages_seen[index]
        if message.get("role") == "tool"
    ]


def round_assistant_tool_calls(
    provider: ScriptedProvider, index: int
) -> list[dict]:
    """第 N 轮发给 LLM 的 assistant.tool_calls 消息。"""
    return [
        message
        for message in provider.messages_seen[index]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]


def roles_of(messages: list[dict]) -> list[str]:
    """取出消息角色序列（便于断言消息顺序）。"""
    return [message.get("role") for message in messages]


def imported_modules(source: str) -> set[str]:
    """用 AST 提取源码中 import 的模块名（不受注释 / 文档字符串干扰）。"""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# ----------------------------------------------------------------------
# 1. 普通文本回答（无 tool_calls）
# ----------------------------------------------------------------------
def test_plain_text_answer() -> None:
    print("\n[1] 普通文本回答（无 tool_calls，不进入 Tool 循环）")
    provider = ScriptedProvider(final_answer="你好，我是企业级 AI Agent 助手。")
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    execution = asyncio.run(
        runtime.execute([{"role": "user", "content": "你好"}])
    )

    check("execute() 返回 AgentExecution",
          isinstance(execution, AgentExecution),
          f"got {type(execution).__name__}")
    check("最终回答来自 LLM",
          execution.final_response == "你好，我是企业级 AI Agent 助手。")
    check("只调用一次 LLM（无 tool_calls 不循环）",
          len(provider.messages_seen) == 1, f"got {len(provider.messages_seen)}")
    check("未走 generate() 兜底路径", provider.generate_calls == 0)
    check("iterations == 0", execution.iterations == 0)
    check("未记录 tool_call / tool_result",
          not execution.used_tools and execution.tool_results == [])
    check("stop_reason == final_answer",
          execution.stop_reason == STOP_REASON_FINAL_ANSWER,
          f"got {execution.stop_reason!r}")
    check("reached_iteration_limit == False",
          execution.reached_iteration_limit is False)
    roles = roles_of(execution.messages)
    check("messages 仅 system + user", roles == ["system", "user"], f"got {roles}")
    check("首条为 system prompt（含内容）",
          execution.messages[0]["role"] == "system"
          and bool(execution.messages[0]["content"]))
    check("tools 已随请求发送（Tool Calling 仍启用）",
          len(provider.tools_seen[0]) == 2,
          f"got {len(provider.tools_seen[0])}")

    # run() 对外契约（返回 str）保持不变
    provider_run = ScriptedProvider(final_answer="你好，我是企业级 AI Agent 助手。")
    reply = asyncio.run(
        AgentRuntime(provider_run, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("run() 仍返回 str 最终回答",
          isinstance(reply, str) and reply == "你好，我是企业级 AI Agent 助手。",
          f"got {reply!r}")

    # 无 Tool 路径回归（registry=None / 空 registry）
    plain = ScriptedProvider(final_answer="普通回答")
    reply_plain = asyncio.run(
        AgentRuntime(plain, tool_registry=None).run(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("registry=None → 走 generate()（零回归）",
          reply_plain == "普通回答"
          and plain.generate_calls == 1
          and not plain.messages_seen,
          f"got {reply_plain!r}")

    empty_registry = ScriptedProvider(final_answer="普通回答")
    execution_empty = asyncio.run(
        AgentRuntime(empty_registry, tool_registry=ToolRegistry()).execute(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("空 Registry → 走 generate()（零回归）",
          execution_empty.final_response == "普通回答"
          and execution_empty.iterations == 0
          and not empty_registry.messages_seen)
    check("无 Tool 路径 stop_reason 仍为 final_answer",
          execution_empty.stop_reason == STOP_REASON_FINAL_ANSWER)


def test_empty_response_boundary() -> None:
    print("\n[1b] 空响应边界（Stage 8 明确定义）")
    none_provider = NoneContentProvider()
    execution_none = asyncio.run(
        AgentRuntime(
            none_provider, tool_registry=build_default_registry()
        ).execute([{"role": "user", "content": "你好"}])
    )
    check("content=None 且无 tool_calls → 兜底文案",
          execution_none.final_response == EMPTY_RESPONSE_FALLBACK,
          f"got {execution_none.final_response!r}")
    check("空响应只调用一次 LLM（不会进入循环）",
          none_provider.with_tools_calls == 1,
          f"got {none_provider.with_tools_calls}")
    check("空响应结束原因仍为 final_answer",
          execution_none.stop_reason == STOP_REASON_FINAL_ANSWER)

    blank = ScriptedProvider(final_answer="   \n  ")
    execution_blank = asyncio.run(
        AgentRuntime(blank, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("纯空白响应 → 兜底文案",
          execution_blank.final_response == EMPTY_RESPONSE_FALLBACK,
          f"got {execution_blank.final_response!r}")

    no_tools_none = NoneContentProvider()
    reply_no_tools = asyncio.run(
        AgentRuntime(no_tools_none, tool_registry=None).run(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("无 Tool 路径的空响应同样兜底",
          reply_no_tools == EMPTY_RESPONSE_FALLBACK
          and no_tools_none.generate_calls == 1,
          f"got {reply_no_tools!r}")

    spaced = ScriptedProvider(final_answer="  保留首尾空格  ")
    spaced_reply = asyncio.run(
        AgentRuntime(spaced, tool_registry=build_default_registry()).run(
            [{"role": "user", "content": "你好"}]
        )
    )
    check("非空回答原样返回（不被改写 / strip）",
          spaced_reply == "  保留首尾空格  ", f"got {spaced_reply!r}")
    check("兜底文案本身非空", bool(EMPTY_RESPONSE_FALLBACK.strip()))


# ----------------------------------------------------------------------
# 2. 单 Tool Calling → Tool Result → Final Answer
# ----------------------------------------------------------------------
def test_single_tool_call() -> None:
    print("\n[2] 单 Tool Calling → Tool Result → Final Answer")
    provider = ScriptedProvider(
        script=[
            ToolCall(
                id="call_calc_1",
                name="calculator",
                arguments={"expression": "25 * 4 + 10"},
            )
        ],
        final_answer="25 * 4 + 10 = 110。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    execution = asyncio.run(
        runtime.execute([{"role": "user", "content": "计算 25 * 4 + 10"}])
    )

    check("最终回答来自第 2 轮 LLM",
          execution.final_response == "25 * 4 + 10 = 110。",
          f"got {execution.final_response!r}")
    check("恰好 2 轮 LLM 调用", len(provider.messages_seen) == 2)
    check("iterations == 1", execution.iterations == 1,
          f"got {execution.iterations}")
    check("stop_reason == final_answer",
          execution.stop_reason == STOP_REASON_FINAL_ANSWER)
    check("used_tools == True", execution.used_tools is True)

    check("状态记录 1 个 tool_call",
          len(execution.tool_calls) == 1
          and execution.tool_calls[0].name == "calculator")
    check("状态记录 1 个 ToolExecution",
          len(execution.tool_results) == 1
          and isinstance(execution.tool_results[0], ToolExecution))
    record = execution.tool_results[0]
    check("tool_result 内容为 110", record.result == "110",
          f"got {record.result!r}")
    check("tool_result 未标记失败", record.failed is False)
    check("tool_result.tool_call_id 正确", record.tool_call_id == "call_calc_1")
    check("tool_result.arguments 为 LLM 原始参数",
          record.arguments == {"expression": "25 * 4 + 10"},
          f"got {record.arguments!r}")

    roles = roles_of(execution.messages)
    check("messages 顺序为 system → user → assistant → tool",
          roles == ["system", "user", "assistant", "tool"], f"got {roles}")

    second_round = provider.messages_seen[1]
    tool_msgs = round_tool_messages(provider, 1)
    assistant_msgs = round_assistant_tool_calls(provider, 1)
    check("第 2 轮把 tool_result 加入 LLM context",
          len(tool_msgs) == 1 and tool_msgs[0]["content"] == "110",
          f"got {tool_msgs[0]['content'] if tool_msgs else None!r}")
    check("第 2 轮 tool 消息 tool_call_id 正确",
          bool(tool_msgs) and tool_msgs[0]["tool_call_id"] == "call_calc_1")
    check("第 2 轮 assistant 消息带 tool_calls", len(assistant_msgs) == 1)
    check("第 2 轮首条仍为 system prompt", second_round[0]["role"] == "system")


# ----------------------------------------------------------------------
# 3. 连续两轮 Tool Calling
# ----------------------------------------------------------------------
def test_two_consecutive_tool_rounds() -> None:
    print("\n[3] 连续两轮 Tool Calling（两个不同 Tool）")
    provider = ScriptedProvider(
        script=[
            ToolCall(
                id="round1_calc",
                name="calculator",
                arguments={"expression": "1 + 2"},
            ),
            ToolCall(
                id="round2_dt",
                name="datetime",
                arguments={"format": "date"},
            ),
        ],
        final_answer="先算出 3，然后取到了今天的日期。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    execution = asyncio.run(
        runtime.execute([{"role": "user", "content": "先算 1+2，再告诉我今天几号"}])
    )

    check("共 3 轮 LLM 调用（tool → tool → final）",
          len(provider.messages_seen) == 3, f"got {len(provider.messages_seen)}")
    check("iterations == 2", execution.iterations == 2,
          f"got {execution.iterations}")
    check("stop_reason == final_answer",
          execution.stop_reason == STOP_REASON_FINAL_ANSWER)
    check("按时间顺序记录 2 个 tool_call",
          [call.name for call in execution.tool_calls]
          == ["calculator", "datetime"],
          f"got {[call.name for call in execution.tool_calls]}")
    check("记录 2 个 tool_result", len(execution.tool_results) == 2)
    check("第 1 轮结果为 3", execution.tool_results[0].result == "3",
          f"got {execution.tool_results[0].result!r}")
    date_value = execution.tool_results[1].result
    check("第 2 轮结果为日期（YYYY-MM-DD）",
          len(date_value) == 10 and date_value.count("-") == 2,
          f"got {date_value!r}")
    check("两轮 tool_call_id 不同且与请求一致",
          [r.tool_call_id for r in execution.tool_results]
          == ["round1_calc", "round2_dt"],
          f"got {[r.tool_call_id for r in execution.tool_results]}")

    roles = roles_of(execution.messages)
    check("messages 顺序交替：assistant.tool_calls → tool（×2）",
          roles == ["system", "user", "assistant", "tool", "assistant", "tool"],
          f"got {roles}")

    third_round = provider.messages_seen[2]
    history_tool_msgs = [
        message for message in third_round if message.get("role") == "tool"
    ]
    history_assistant_msgs = [
        message
        for message in third_round
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    check("第 3 轮携带两轮 tool 结果（多轮上下文完整）",
          len(history_tool_msgs) == 2, f"got {len(history_tool_msgs)}")
    check("第 3 轮携带两条 assistant.tool_calls 消息",
          len(history_assistant_msgs) == 2,
          f"got {len(history_assistant_msgs)}")
    check("第 3 轮 tool_call_id 与请求一一对应",
          [message["tool_call_id"] for message in history_tool_msgs]
          == ["round1_calc", "round2_dt"],
          f"got {[m['tool_call_id'] for m in history_tool_msgs]}")
    check("第 3 轮首条仍为 system prompt", third_round[0]["role"] == "system")
    check("第 2 轮只携带第 1 轮的结果（不提前插入）",
          len(round_tool_messages(provider, 1)) == 1
          and round_tool_messages(provider, 1)[0]["tool_call_id"] == "round1_calc")


# ----------------------------------------------------------------------
# 4. 同一轮多个 Tool Calls
# ----------------------------------------------------------------------
def test_multiple_tool_calls_in_one_round() -> None:
    print("\n[4] 同一轮多个 Tool Calls")

    class TwoCallsProvider(LLMProvider):
        """第 1 轮一次返回两个 tool_calls，第 2 轮给出最终回答。"""

        def __init__(self) -> None:
            self.rounds = 0
            self.messages_seen: list[list[dict]] = []

        async def generate(self, messages: list[dict]) -> str:
            return "两个值都取到了。"

        async def generate_with_tools(
            self, messages: list[dict], tools: list[dict]
        ) -> LLMResponse:
            self.rounds += 1
            self.messages_seen.append(list(messages))
            if self.rounds == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="multi_1", name="spy",
                                 arguments={"value": 1}),
                        ToolCall(id="multi_2", name="spy",
                                 arguments={"value": 2}),
                    ],
                )
            return LLMResponse(content="两个值都取到了。")

    spy = SpyTool()
    provider = TwoCallsProvider()
    runtime = AgentRuntime(provider, tool_registry=ToolRegistry(tools=[spy]))
    execution = asyncio.run(
        runtime.execute([{"role": "user", "content": "取两个值"}])
    )

    check("最终回答来自第 2 轮",
          execution.final_response == "两个值都取到了。",
          f"got {execution.final_response!r}")
    check("iterations == 1（两个调用属于同一轮）",
          execution.iterations == 1, f"got {execution.iterations}")
    check("同轮两个 tool_call 都执行且只执行一次",
          spy.calls == [{"value": 1}, {"value": 2}], f"got {spy.calls}")

    assistant_msgs = round_assistant_tool_calls(provider, 1)
    check("只产生 1 条 assistant 消息（内含 2 个 tool_calls）",
          len(assistant_msgs) == 1, f"got {len(assistant_msgs)}")
    check("assistant.tool_calls 含 2 个调用",
          bool(assistant_msgs)
          and [call["function"]["name"]
               for call in assistant_msgs[0]["tool_calls"]]
          == ["spy", "spy"])

    tool_msgs = round_tool_messages(provider, 1)
    check("产生 2 条 tool 结果消息（顺序与请求一致）",
          [message["content"] for message in tool_msgs] == ["spy:1", "spy:2"],
          f"got {[m['content'] for m in tool_msgs]}")
    check("两条 tool 结果的 tool_call_id 不同且正确",
          [message["tool_call_id"] for message in tool_msgs]
          == ["multi_1", "multi_2"],
          f"got {[m['tool_call_id'] for m in tool_msgs]}")
    check("状态记录 2 个 tool_result", len(execution.tool_results) == 2)
    roles = roles_of(execution.messages)
    check("messages 顺序 system → user → assistant → tool → tool",
          roles == ["system", "user", "assistant", "tool", "tool"],
          f"got {roles}")


# ----------------------------------------------------------------------
# 5. Unknown Tool
# ----------------------------------------------------------------------
def test_unknown_tool() -> None:
    print("\n[5] Unknown Tool（UnknownToolError）")
    registry = build_probe_registry()
    provider = ScriptedProvider(
        script=[ToolCall(id="ghost_1", name="ghost_tool", arguments={})],
        final_answer="该工具不可用，我已向用户说明。",
    )
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=registry).execute(
            [{"role": "user", "content": "调用一个不存在的工具"}]
        )
    )

    record = execution.tool_results[0]
    check("未知工具不中断会话（仍返回最终回答）",
          execution.final_response == "该工具不可用，我已向用户说明。",
          f"got {execution.final_response!r}")
    check("错误以 'Tool error: ' 前缀回传",
          record.result.startswith(TOOL_ERROR_PREFIX), f"got {record.result!r}")
    check("错误信息含 'Unknown tool'", "Unknown tool" in record.result)
    check("错误信息列出可用工具（便于模型自我纠正）",
          "spy" in record.result and "boom" in record.result,
          f"got {record.result!r}")
    check("状态标记该次调用失败", record.failed is True)
    check("Tool 名与 tool_call_id 正确记录",
          record.name == "ghost_tool" and record.tool_call_id == "ghost_1")

    raised: UnknownToolError | None = None
    try:
        registry.get("ghost_tool")
    except UnknownToolError as exc:
        raised = exc
    check("Registry.get 抛 UnknownToolError（Tool 层职责，Runtime 不判断业务）",
          raised is not None and "ghost_tool" in raised.message)

    tool_msgs = round_tool_messages(provider, 1)
    check("错误结果被加入第 2 轮 LLM context",
          len(tool_msgs) == 1 and tool_msgs[0]["content"] == record.result)
    check("恰好 2 轮 LLM 调用（错误后仍继续对话）",
          len(provider.messages_seen) == 2)
    check("iterations == 1", execution.iterations == 1)


# ----------------------------------------------------------------------
# 6. 参数校验失败
# ----------------------------------------------------------------------
def test_parameter_validation_failure() -> None:
    print("\n[6] 参数校验失败（ToolParameterError）")
    cases = [
        (
            "缺少必填参数",
            ToolCall(id="p_missing", name="spy", arguments={}),
            "缺少必填参数",
        ),
        (
            "参数类型错误",
            ToolCall(id="p_type", name="spy", arguments={"value": "abc"}),
            "类型错误",
        ),
        (
            "未定义参数",
            ToolCall(id="p_extra", name="spy",
                     arguments={"value": 1, "extra": 2}),
            "不支持未定义参数",
        ),
        (
            "arguments 不是 JSON 对象",
            ToolCall(id="p_not_object", name="spy", arguments=[]),  # type: ignore[arg-type]
            "缺少必填参数",
        ),
    ]

    for label, call, expected in cases:
        spy = SpyTool()
        provider = ScriptedProvider(
            script=[call], final_answer="已向用户说明参数不合法。"
        )
        execution = asyncio.run(
            AgentRuntime(
                provider, tool_registry=ToolRegistry(tools=[spy])
            ).execute([{"role": "user", "content": "触发参数错误"}])
        )
        record = execution.tool_results[0]

        check(f"{label}：会话不中断（仍返回最终回答）",
              execution.final_response == "已向用户说明参数不合法。",
              f"got {execution.final_response!r}")
        check(f"{label}：以 'Tool error: ' 回传",
              record.result.startswith(TOOL_ERROR_PREFIX),
              f"got {record.result!r}")
        check(f"{label}：错误信息含「{expected}」",
              expected in record.result, f"got {record.result!r}")
        check(f"{label}：标记为失败", record.failed is True)
        check(f"{label}：校验失败时 Tool.execute() 未被调用",
              spy.calls == [], f"got {spy.calls}")
        check(f"{label}：结束原因仍为 final_answer",
              execution.stop_reason == STOP_REASON_FINAL_ANSWER)

    check("ToolParameterError 与 ToolExecutionError 语义分离",
          issubclass(ToolParameterError, ToolError)
          and not issubclass(ToolParameterError, ToolExecutionError))


# ----------------------------------------------------------------------
# 7. Tool execution error
# ----------------------------------------------------------------------
def test_tool_execution_error() -> None:
    print("\n[7] Tool execution error（Tool 层失败不中断 Runtime）")
    registry = build_probe_registry()
    cases = [
        (
            "ToolExecutionError",
            ToolCall(id="e_boom", name="boom", arguments={}),
            "boom 工具执行失败",
        ),
        (
            "非预期异常（ValueError）",
            ToolCall(id="e_crash", name="crash", arguments={}),
            "未预期错误",
        ),
        (
            "返回 None",
            ToolCall(id="e_none", name="none_result", arguments={}),
            "未返回结果",
        ),
    ]

    for label, call, expected in cases:
        provider = ScriptedProvider(
            script=[call], final_answer="已向用户说明工具执行失败。"
        )
        execution = asyncio.run(
            AgentRuntime(provider, tool_registry=registry).execute(
                [{"role": "user", "content": "触发工具执行错误"}]
            )
        )
        record = execution.tool_results[0]

        check(f"{label}：不中断（仍返回最终回答）",
              execution.final_response == "已向用户说明工具执行失败。",
              f"got {execution.final_response!r}")
        check(f"{label}：以 'Tool error: ' 回传",
              record.result.startswith(TOOL_ERROR_PREFIX),
              f"got {record.result!r}")
        check(f"{label}：错误信息含「{expected}」",
              expected in record.result, f"got {record.result!r}")
        check(f"{label}：标记为失败", record.failed is True)
        check(f"{label}：tool_call_id 仍正确传递",
              record.tool_call_id == call.id, f"got {record.tool_call_id!r}")

    # 非预期异常的错误信息带上异常类型名（便于定位 Tool 实现问题）
    crash_provider = ScriptedProvider(
        script=[ToolCall(id="e_crash2", name="crash", arguments={})],
        final_answer="ok",
    )
    crash_execution = asyncio.run(
        AgentRuntime(crash_provider, tool_registry=registry).execute(
            [{"role": "user", "content": "x"}]
        )
    )
    check("非预期异常错误信息含异常类型名（ValueError）",
          "ValueError" in crash_execution.tool_results[0].result,
          f"got {crash_execution.tool_results[0].result!r}")

    # 非字符串返回值归一化
    int_provider = ScriptedProvider(
        script=[ToolCall(id="e_int", name="int_result", arguments={})],
        final_answer="ok",
    )
    int_execution = asyncio.run(
        AgentRuntime(int_provider, tool_registry=registry).execute(
            [{"role": "user", "content": "x"}]
        )
    )
    check("int 返回值归一化为字符串 '42'",
          int_execution.tool_results[0].result == "42"
          and int_execution.tool_results[0].failed is False,
          f"got {int_execution.tool_results[0].result!r}")
    check("tool_result 始终是 str（messages 契约）",
          all(isinstance(record.result, str)
              for record in int_execution.tool_results))

    # 任务取消（BaseException）不被当作 Tool 错误吞掉
    cancel_provider = ScriptedProvider(
        script=[ToolCall(id="e_cancel", name="cancel", arguments={})],
        final_answer="ok",
    )
    cancel_error: BaseException | None = None
    try:
        asyncio.run(
            AgentRuntime(cancel_provider, tool_registry=registry).execute(
                [{"role": "user", "content": "x"}]
            )
        )
    except BaseException as exc:  # noqa: BLE001 - 仅判断是否向上传播
        cancel_error = exc
    check("CancelledError 不被吞掉（任务取消仍然生效）",
          isinstance(cancel_error, asyncio.CancelledError),
          f"got {cancel_error!r}")


# ----------------------------------------------------------------------
# 8. 最大 iteration 限制
# ----------------------------------------------------------------------
def test_max_iterations() -> None:
    print("\n[8] 最大 iteration 限制（防无限循环）")
    check("默认上限为 3", MAX_TOOL_ITERATIONS == 3,
          f"got {MAX_TOOL_ITERATIONS}")
    default_runtime = AgentRuntime(
        ScriptedProvider(), tool_registry=build_default_registry()
    )
    check("Runtime 默认使用该上限",
          default_runtime.max_iterations == MAX_TOOL_ITERATIONS)

    provider = AlwaysToolProvider()
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "一直调用工具"}]
        )
    )

    check("工具轮数被限制为 3（不存在无限循环）",
          provider.tool_rounds == 3, f"got {provider.tool_rounds}")
    check("iterations == max_iterations",
          execution.iterations == MAX_TOOL_ITERATIONS,
          f"got {execution.iterations}")
    check("stop_reason == max_iterations",
          execution.stop_reason == STOP_REASON_MAX_ITERATIONS,
          f"got {execution.stop_reason!r}")
    check("reached_iteration_limit == True",
          execution.reached_iteration_limit is True)
    check("达到上限后仍返回最终回答（兜底 generate）",
          execution.final_response == "兜底最终回答"
          and provider.generate_calls == 1,
          f"got {execution.final_response!r}")
    check("3 轮各执行一次 Tool（无重复执行路径）",
          [record.result for record in execution.tool_results] == ["2", "2", "2"],
          f"got {[r.result for r in execution.tool_results]}")
    check("3 轮 tool_call_id 各不相同",
          len({call.id for call in execution.tool_calls}) == 3,
          f"got {[c.id for c in execution.tool_calls]}")
    check("兜底 generate 收到完整会话（最后一条为 tool 结果）",
          provider.messages_seen[0][-1]["role"] == "tool",
          f"got {provider.messages_seen[0][-1].get('role')!r}")
    check("兜底 generate 收到 3 条 tool 结果",
          len([m for m in provider.messages_seen[0] if m.get("role") == "tool"])
          == 3)
    check("messages 为 system → user → (assistant → tool) ×3",
          roles_of(execution.messages)
          == ["system", "user"] + ["assistant", "tool"] * 3,
          f"got {roles_of(execution.messages)}")

    # 自定义上限（测试严格控制边界）
    one_provider = AlwaysToolProvider()
    one_execution = asyncio.run(
        AgentRuntime(
            one_provider,
            tool_registry=build_default_registry(),
            max_iterations=1,
        ).execute([{"role": "user", "content": "一直调用工具"}])
    )
    check("max_iterations=1 → 只执行 1 轮工具调用",
          one_provider.tool_rounds == 1, f"got {one_provider.tool_rounds}")
    check("max_iterations=1 → iterations / stop_reason 正确",
          one_execution.iterations == 1
          and one_execution.stop_reason == STOP_REASON_MAX_ITERATIONS)

    # 非法上限明确报错（不静默回落）
    for bad in (0, -1):
        raised = False
        try:
            AgentRuntime(
                ScriptedProvider(),
                tool_registry=build_default_registry(),
                max_iterations=bad,
            )
        except ValueError:
            raised = True
        check(f"max_iterations={bad} → ValueError", raised)


# ----------------------------------------------------------------------
# 9. 最终回答返回逻辑
# ----------------------------------------------------------------------
def test_final_answer_contract() -> None:
    print("\n[9] 最终回答返回逻辑")
    script = [
        ToolCall(id="fa_1", name="calculator", arguments={"expression": "2 + 3"}),
        ToolCall(id="fa_2", name="datetime", arguments={"format": "date"}),
    ]
    execution = asyncio.run(
        AgentRuntime(
            ScriptedProvider(script=script, final_answer="2 + 3 = 5，今天是某天。"),
            tool_registry=build_default_registry(),
        ).execute([{"role": "user", "content": "先算 2+3，再说日期"}])
    )
    reply = asyncio.run(
        AgentRuntime(
            ScriptedProvider(script=script, final_answer="2 + 3 = 5，今天是某天。"),
            tool_registry=build_default_registry(),
        ).run([{"role": "user", "content": "先算 2+3，再说日期"}])
    )

    check("run() 与 execute().final_response 完全一致",
          reply == execution.final_response, f"got {reply!r}")
    check("多次 Tool Calling 后返回 LLM 的最终回答（非 tool_result 拼接）",
          execution.final_response == "2 + 3 = 5，今天是某天。",
          f"got {execution.final_response!r}")
    check("final_response 一定是非空 str",
          isinstance(execution.final_response, str)
          and bool(execution.final_response))
    check("执行结束后 stop_reason 一定已设置",
          execution.stop_reason is not None)
    check("返回回答时不再携带 tools 继续循环（iterations == 2 + 1 轮回答）",
          execution.iterations == 2 and len(execution.tool_results) == 2)


# ----------------------------------------------------------------------
# 10. 原始 messages 没有被错误污染
# ----------------------------------------------------------------------
def test_original_messages_not_polluted() -> None:
    print("\n[10] 原始 messages 没有被错误污染")
    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "计算 2 + 3"},
    ]
    snapshot = copy.deepcopy(history)
    script = [
        ToolCall(id="clean_1", name="calculator",
                 arguments={"expression": "2 + 3"})
    ]
    provider = ScriptedProvider(script=script, final_answer="2 + 3 = 5。")
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).execute(
            history, system_prompt="自定义 system 提示词"
        )
    )

    check("调用方 messages 长度不变", len(history) == 3,
          f"got {len(history)}")
    check("调用方 messages 内容完全未变", history == snapshot,
          f"got {history!r}")
    check("调用方消息字典未被插入 tool_calls 字段",
          all("tool_calls" not in message for message in history))
    check("调用方消息未被追加 tool 结果",
          roles_of(history) == ["user", "assistant", "user"],
          f"got {roles_of(history)}")
    check("execution.messages 与调用方不是同一列表对象",
          execution.messages is not history)
    check("execution.messages 是新列表且更完整（6 条）",
          len(execution.messages) == 6 and len(history) == 3,
          f"got {len(execution.messages)}")
    check("历史消息对象被复用（Runtime 只读，不复制也不修改）",
          execution.messages[1] is history[0])
    check("system_prompt 只存在于执行副本中",
          execution.messages[0]["content"] == "自定义 system 提示词"
          and all(message.get("role") != "system" for message in history))

    # 同一 history 再次执行不会累积污染
    provider2 = ScriptedProvider(
        script=list(script), final_answer="2 + 3 = 5。"
    )
    execution2 = asyncio.run(
        AgentRuntime(provider2, tool_registry=build_default_registry()).execute(
            history
        )
    )
    check("重复执行仍不污染原始 history",
          history == snapshot and len(execution2.messages) == 6,
          f"got {history!r}")
    check("两次执行的 messages 互相独立",
          execution.messages is not execution2.messages)


# ----------------------------------------------------------------------
# 11. tool_call_id / tool name / arguments 正确传递
# ----------------------------------------------------------------------
def test_tool_call_identity_and_arguments() -> None:
    print("\n[11] tool_call_id / tool name / arguments 正确传递")
    original_arguments = {"expression": "25 * 4 + 10"}
    arguments_snapshot = copy.deepcopy(original_arguments)
    call = ToolCall(
        id="integrity_1", name="calculator", arguments=original_arguments
    )
    provider = ScriptedProvider(script=[call], final_answer="= 110。")
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "计算"}]
        )
    )

    assistant_msgs = round_assistant_tool_calls(provider, 1)
    tool_msgs = round_tool_messages(provider, 1)
    check("assistant 消息只含 1 个 tool_call",
          len(assistant_msgs) == 1
          and len(assistant_msgs[0]["tool_calls"]) == 1)
    openai_call = assistant_msgs[0]["tool_calls"][0]

    check("tool_call_id 在 assistant / tool 消息之间匹配",
          openai_call["id"] == "integrity_1"
          and tool_msgs[0]["tool_call_id"] == "integrity_1",
          f"got {openai_call['id']!r} / {tool_msgs[0]['tool_call_id']!r}")
    check("tool_call type == 'function'", openai_call["type"] == "function")
    check("tool name 原样回传",
          openai_call["function"]["name"] == "calculator")
    check("arguments 序列化后与原参数一致（未被修改）",
          json.loads(openai_call["function"]["arguments"])
          == {"expression": "25 * 4 + 10"},
          f"got {openai_call['function']['arguments']!r}")
    check("ToolCall.arguments 对象未被 Runtime 修改",
          original_arguments == arguments_snapshot, f"got {original_arguments!r}")
    check("ToolCall.name 未被 Runtime 修改", call.name == "calculator")
    check("每个 tool_call_id 恰好对应一条 tool 消息",
          [openai_call["id"]] == [message["tool_call_id"] for message in tool_msgs])
    check("状态记录与消息中的 id 一致",
          execution.tool_results[0].tool_call_id == "integrity_1")

    # 多轮时 id 不串轮、顺序不乱
    provider2 = ScriptedProvider(
        script=[
            ToolCall(id="chain_a", name="calculator",
                     arguments={"expression": "1 + 1"}),
            ToolCall(id="chain_b", name="calculator",
                     arguments={"expression": "2 + 2"}),
        ],
        final_answer="ok",
    )
    execution2 = asyncio.run(
        AgentRuntime(provider2, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "两轮计算"}]
        )
    )
    third_round = provider2.messages_seen[2]
    assistant_ids = [
        call_dict["id"]
        for message in third_round
        if message.get("role") == "assistant" and message.get("tool_calls")
        for call_dict in message["tool_calls"]
    ]
    tool_ids = [
        message["tool_call_id"]
        for message in third_round
        if message.get("role") == "tool"
    ]
    check("跨轮 tool_call_id 顺序一致",
          assistant_ids == tool_ids == ["chain_a", "chain_b"],
          f"got {assistant_ids} / {tool_ids}")
    check("状态记录与两轮 id 一致",
          [call.id for call in execution2.tool_calls] == ["chain_a", "chain_b"])
    check("tool_results 保留各轮原始参数",
          [record.arguments for record in execution2.tool_results]
          == [{"expression": "1 + 1"}, {"expression": "2 + 2"}],
          f"got {[r.arguments for r in execution2.tool_results]}")


# ----------------------------------------------------------------------
# 12. Provider 错误边界
# ----------------------------------------------------------------------
def test_provider_error_boundary() -> None:
    print("\n[12] Provider 错误边界")

    # 1) generate_with_tools 抛异常 → 原样向上传播（不吞掉、不伪造回答）
    provider_error: RuntimeError | None = None
    try:
        asyncio.run(
            AgentRuntime(
                ExplodingProvider(), tool_registry=build_default_registry()
            ).run([{"role": "user", "content": "hi"}])
        )
    except RuntimeError as exc:
        provider_error = exc
    check("Provider 异常原样向上传播（Runtime 不吞掉）",
          isinstance(provider_error, RuntimeError)
          and "provider 网络故障" in str(provider_error),
          f"got {provider_error!r}")

    # 2) 达到上限后的兜底 generate 抛异常 → 同样向上传播
    fallback_error: RuntimeError | None = None
    try:
        asyncio.run(
            AgentRuntime(
                ExplodingFallbackProvider(),
                tool_registry=build_default_registry(),
            ).run([{"role": "user", "content": "hi"}])
        )
    except RuntimeError as exc:
        fallback_error = exc
    check("兜底 generate 异常同样向上传播",
          isinstance(fallback_error, RuntimeError)
          and "兜底调用失败" in str(fallback_error),
          f"got {fallback_error!r}")

    # 3) LLMResponse 语义（正常文本 / tool_calls）
    check("LLMResponse.has_tool_calls 语义正确",
          LLMResponse(content="回答").has_tool_calls is False
          and LLMResponse(
              tool_calls=[ToolCall(id="1", name="n", arguments={})]
          ).has_tool_calls is True)

    # 4) malformed provider 响应：由 Provider 解析层明确报错
    #    （Runtime 不猜测非法响应，也不会因此进入异常循环）
    malformed_cases = [
        {
            "choices": [{"message": {"tool_calls": [
                {"function": {"name": "x", "arguments": "not-json"}}
            ]}}]
        },
        {"choices": [{"message": {"tool_calls": ["not-a-dict"]}}]},
        {"bad": "data"},
    ]
    for index, payload in enumerate(malformed_cases, start=1):
        raised = False
        try:
            parse_chat_completion(payload, provider_label="Test")
        except RuntimeError:
            raised = True
        check(f"malformed provider 响应 #{index} 由解析层明确报错",
              raised, f"payload={payload!r}")

    # 5) tool_calls 与文本同时存在时以 tool_calls 为准（继续循环）
    provider = ScriptedProvider(
        script=[
            ToolCall(id="both_1", name="calculator",
                     arguments={"expression": "3 + 4"})
        ],
        final_answer="3 + 4 = 7。",
    )
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "3+4"}]
        )
    )
    check("有 tool_calls 时继续循环（不会被文本提前结束）",
          execution.iterations == 1
          and execution.tool_results[0].result == "7"
          and execution.final_response == "3 + 4 = 7。",
          f"got {execution.final_response!r}")


# ----------------------------------------------------------------------
# 13. 执行状态语义
# ----------------------------------------------------------------------
def test_execution_state_semantics() -> None:
    print("\n[13] 执行状态（AgentExecution / ToolExecution）")
    provider = ScriptedProvider(
        script=[
            ToolCall(id="s1", name="calculator",
                     arguments={"expression": "9 * 9"})
        ],
        final_answer="81。",
    )
    execution = asyncio.run(
        AgentRuntime(provider, tool_registry=build_default_registry()).execute(
            [{"role": "user", "content": "9 * 9"}]
        )
    )

    check("stop_reason 常量值稳定",
          STOP_REASON_FINAL_ANSWER == "final_answer"
          and STOP_REASON_MAX_ITERATIONS == "max_iterations")
    check("Tool 错误前缀常量稳定", TOOL_ERROR_PREFIX == "Tool error:")
    check("execution.max_iterations 记录本次上限",
          execution.max_iterations == MAX_TOOL_ITERATIONS,
          f"got {execution.max_iterations}")
    check("execution.messages 为 list[dict]",
          isinstance(execution.messages, list)
          and all(isinstance(message, dict) for message in execution.messages))
    check("execution.tool_calls 为 list[ToolCall]",
          isinstance(execution.tool_calls, list)
          and all(isinstance(call, ToolCall) for call in execution.tool_calls))
    check("execution.tool_results 与 tool_calls 数量一致",
          len(execution.tool_results) == len(execution.tool_calls) == 1)
    check("ToolExecution.failed 依据统一前缀判断",
          ToolExecution(tool_call_id="x", name="n", arguments={},
                        result="Tool error: x").failed is True
          and ToolExecution(tool_call_id="x", name="n", arguments={},
                            result="ok").failed is False)

    fresh = AgentExecution(messages=[], max_iterations=1)
    check("未执行状态为空（iterations / final_response / stop_reason）",
          fresh.iterations == 0 and fresh.final_response == ""
          and fresh.stop_reason is None and fresh.used_tools is False
          and fresh.reached_iteration_limit is False)
    check("执行状态是轻量 dataclass",
          dataclasses.is_dataclass(AgentExecution)
          and dataclasses.is_dataclass(ToolExecution))

    reached = asyncio.run(
        AgentRuntime(
            AlwaysToolProvider(),
            tool_registry=build_default_registry(),
            max_iterations=1,
        ).execute([{"role": "user", "content": "一直调用"}])
    )
    check("达到上限的终态：stop_reason 已设置 + reached_iteration_limit",
          reached.stop_reason is not None
          and reached.reached_iteration_limit is True
          and reached.used_tools is True)


# ----------------------------------------------------------------------
# 14. 架构边界静态检查
# ----------------------------------------------------------------------
def test_architecture_boundaries() -> None:
    print("\n[14] 架构边界静态检查")
    runtime_source = (AGENTS_DIR / "runtime.py").read_text(encoding="utf-8")
    execution_source = (AGENTS_DIR / "execution.py").read_text(encoding="utf-8")
    service_source = (
        BACKEND_DIR / "app" / "services" / "agent.py"
    ).read_text(encoding="utf-8")
    dependencies_source = (
        BACKEND_DIR / "app" / "core" / "dependencies.py"
    ).read_text(encoding="utf-8")

    runtime_imports = imported_modules(runtime_source)
    # Stage 9 起 Runtime 额外依赖 Observer 抽象（app.agents.observers）；
    # 断言仍保持「精确集合」语义：除下表之外不得引入任何新模块。
    check("runtime.py 仍只依赖 Provider / Tool 抽象、执行状态与 Observer 抽象",
          runtime_imports == {
              "app.agents.execution",
              "app.agents.observers",
              "app.agents.providers",
              "app.agents.providers.base",
              "app.agents.tools.base",
              "app.agents.tools.registry",
          },
          f"got {sorted(runtime_imports)}")

    forbidden = (
        "fastapi", "sqlalchemy", "app.database", "app.rag", "chromadb",
        "redis", "celery", "langchain", "langgraph",
        "app.agents.tools.builtin",
    )
    violations = [
        module for module in runtime_imports
        if any(module == item or module.startswith(item + ".")
               for item in forbidden)
    ]
    check("runtime.py 不依赖 FastAPI / DB / RAG / 具体 Tool / 外部框架",
          not violations, f"{violations}")
    check("runtime.py 不出现具体 Tool 实现名",
          not any(term in runtime_source
                  for term in ("CalculatorTool", "DateTimeTool")),
          "runtime.py 出现具体 Tool 名")
    check("runtime.py 仍导入 ToolRegistry / ToolError（分层未变）",
          "from app.agents.tools.registry import ToolRegistry" in runtime_source
          and "from app.agents.tools.base import ToolError" in runtime_source)

    execution_imports = imported_modules(execution_source)
    # Stage 9 起用标准库 time.perf_counter() 统计执行耗时（time 属标准库）。
    check("execution.py 仅依赖标准库 + Provider 数据结构",
          execution_imports == {
              "dataclasses", "time", "app.agents.providers.base",
          },
          f"got {sorted(execution_imports)}")
    check("execution.py 无流程控制逻辑（无 while / for 循环）",
          not any(
              isinstance(node, (ast.While, ast.For, ast.AsyncFor))
              for node in ast.walk(ast.parse(execution_source))
          ))

    # Stage 17：Service 按 Agent 配置选择 Runtime（self.runtime 或按 Agent 构建的
    # runtime），但调用契约不变 —— 仍是 runtime.run(messages, system_prompt=...)
    check("Service 仍以 runtime.run(messages, system_prompt=...) 调用（契约未变）",
          "runtime.run(messages, system_prompt=system_prompt)"
          in service_source)
    check("Service 不感知 AgentExecution / stop_reason（内部状态不外泄）",
          "AgentExecution" not in service_source
          and "stop_reason" not in service_source)
    check("DI 仍以 AgentRuntime(provider, tool_registry=...) 构建",
          "AgentRuntime(" in dependencies_source
          and "get_tool_registry()" in dependencies_source
          and "tool_registry=registry" in dependencies_source)
    check("未引入被禁止的外部组件（LangChain / LangGraph / Redis / Celery / 事件总线）",
          not any(
              term in runtime_source + execution_source
              for term in ("langchain", "langgraph", "redis", "celery",
                           "EventBus", "Workflow")
          ))


def main() -> None:
    test_plain_text_answer()
    test_empty_response_boundary()
    test_single_tool_call()
    test_two_consecutive_tool_rounds()
    test_multiple_tool_calls_in_one_round()
    test_unknown_tool()
    test_parameter_validation_failure()
    test_tool_execution_error()
    test_max_iterations()
    test_final_answer_contract()
    test_original_messages_not_polluted()
    test_tool_call_identity_and_arguments()
    test_provider_error_boundary()
    test_execution_state_semantics()
    test_architecture_boundaries()

    print("\n" + "=" * 56)
    print("Stage 8 Agent Execution Loop acceptance: "
          f"{'PASS' if not failed else 'FAIL'}")
    print(f"通过: {len(passed)}  失败: {len(failed)}")
    if failed:
        print("失败项:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通过！")


if __name__ == "__main__":
    main()












