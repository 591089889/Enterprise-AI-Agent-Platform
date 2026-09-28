"""Stage 7：多 Tool + Tool 参数校验 验收测试（进程内，不依赖数据库 / 服务器）。

覆盖：

1. 多 Tool Registry：build_default_registry 真正注册多个 Tool，
   list_tools() / get_schemas() 同时返回多个结果
2. 第二个 Tool（DateTimeTool）行为与参数
3. 参数校验（Schema 驱动，ToolRegistry.execute() 统一入口）：
   缺失 / 类型错误 / 未定义参数 / enum 非法 / 非 JSON 对象
4. Tool Calling 全流程（Mock Provider）：
   LLM → tool_call → Runtime → Registry → Tool → tool_result → LLM → final answer
   （Calculator 与 DateTime 两个 Tool）
5. 错误处理：Tool 错误以 tool_result 回传，不破坏 Agent 会话
6. 安全回归：Calculator 仍然拒绝任意 Python 代码（无 eval / exec）
7. 架构边界静态检查：Registry / Runtime 不依赖具体 Tool 实现

运行方式（在 backend/ 目录下）::

    python -m scripts.tool_validation_acceptance_test

本脚本不读取 / 打印 backend/.env 中的任何敏感值。
"""

import ast
import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.agents.providers.base import LLMProvider, LLMResponse, ToolCall
from app.agents.runtime import AgentRuntime
from app.agents.tools import (
    CalculatorTool,
    DateTimeTool,
    Tool,
    ToolError,
    ToolExecutionError,
    ToolParameterError,
    ToolRegistry,
    UnknownToolError,
    build_default_registry,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BACKEND_DIR / "app" / "agents" / "tools"

#: 供断言使用的正则
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}$")

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
# 测试用 Tool（仅用于验证校验逻辑，不进入 build_default_registry）
# ----------------------------------------------------------------------
class SpyTool(Tool):
    """记录 execute 调用情况的测试工具（含必填 integer 参数）。"""

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


class TypesProbeTool(Tool):
    """覆盖所有受支持 JSON 类型的测试工具（无必填参数）。"""

    name = "types_probe"
    description = "测试用工具（类型校验）"
    parameters = {
        "type": "object",
        "properties": {
            "s": {"type": "string"},
            "i": {"type": "integer"},
            "n": {"type": "number"},
            "b": {"type": "boolean"},
            "a": {"type": "array"},
            "o": {"type": "object"},
        },
        "required": [],
    }

    async def execute(self, arguments: dict) -> str:
        return "types-ok"


class ExtraAllowedTool(Tool):
    """显式声明 additionalProperties=true 的测试工具。"""

    name = "extra_allowed"
    description = "测试用工具（允许额外参数）"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": True,
    }

    async def execute(self, arguments: dict) -> str:
        return f"extra:{arguments['text']}"


def expect_parameter_error(
    registry: ToolRegistry, name: str, arguments: object
) -> str | None:
    """执行 Tool 并期望 ToolParameterError；未抛出时返回 None。"""
    try:
        asyncio.run(registry.execute(name, arguments))  # type: ignore[arg-type]
    except ToolParameterError as exc:
        return exc.message
    return None


def window(
    lower: datetime, upper: datetime, fmt: str, offset_days: int = 0
) -> tuple[str, str]:
    """构造「允许的时间窗口」（把下界/上界按 offset_days 平移）。"""
    return (
        (lower + timedelta(days=offset_days)).strftime(fmt),
        (upper + timedelta(days=offset_days)).strftime(fmt),
    )


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
# 1. 多 Tool Registry
# ----------------------------------------------------------------------
def test_multi_tool_registry() -> None:
    print("\n[1] 多 Tool Registry（build_default_registry）")
    registry = build_default_registry()

    check("注册表包含 2 个内置 Tool", len(registry) == 2, f"got {len(registry)}")
    check("get('calculator') → CalculatorTool",
          isinstance(registry.get("calculator"), CalculatorTool))
    check("get('datetime') → DateTimeTool",
          isinstance(registry.get("datetime"), DateTimeTool))
    check("'calculator' 可通过 in 判断", "calculator" in registry)
    check("'datetime' 可通过 in 判断", "datetime" in registry)
    check("_tools 是以 name 为键的 dict（保存 Tool 实例）",
          isinstance(registry._tools, dict)
          and set(registry._tools) == {"calculator", "datetime"},
          f"got {getattr(registry, '_tools', None)}")

    names = [tool.name for tool in registry.list_tools()]
    check("list_tools 返回多个 Tool（稳定排序）",
          names == ["calculator", "datetime"], f"got {names}")

    schemas = registry.get_schemas()
    check("get_schemas 返回多个 Schema", len(schemas) == 2,
          f"got {len(schemas)}")
    schema_names = [schema["function"]["name"] for schema in schemas]
    check("Schema 顺序与 list_tools 一致",
          schema_names == ["calculator", "datetime"], f"got {schema_names}")
    for schema in schemas:
        function = schema["function"]
        check(f"Schema 完整（{function['name']}）：type/description/parameters",
              schema["type"] == "function"
              and bool(function["description"])
              and function["parameters"]["type"] == "object")

    # Registry 不限定 Tool 数量：可继续注册第三个（自定义）Tool
    extended = ToolRegistry(tools=[CalculatorTool(), DateTimeTool()])
    extended.register(SpyTool())
    check("可注册第三个自定义 Tool 且顺序稳定",
          [tool.name for tool in extended.list_tools()]
          == ["calculator", "datetime", "spy"])

    check("多 Tool 下 execute 按 name 路由到 calculator",
          asyncio.run(extended.execute("calculator", {"expression": "6 * 7"}))
          == "42")
    check("多 Tool 下 execute 按 name 路由到 datetime",
          bool(DATETIME_PATTERN.match(
              asyncio.run(extended.execute("datetime", {})))))
    check("多 Tool 下 execute 按 name 路由到 spy",
          asyncio.run(extended.execute("spy", {"value": 7})) == "spy:7")


# ----------------------------------------------------------------------
# 2. 第二个 Tool：DateTimeTool
# ----------------------------------------------------------------------
def test_datetime_tool() -> None:
    print("\n[2] 第二个 Tool：DateTimeTool")
    registry = build_default_registry()

    function = DateTimeTool().get_schema()["function"]
    check("datetime Schema.name", function["name"] == "datetime")
    check("datetime Schema.description 非空", bool(function["description"]))
    parameters = function["parameters"]
    check("datetime 无必填参数（required 为空）",
          parameters.get("required") == [])
    check("datetime 声明 format / offset_days",
          sorted(parameters["properties"]) == ["format", "offset_days"],
          f"got {sorted(parameters['properties'])}")

    # 默认格式 datetime（无参数调用）
    lower = datetime.now()
    default_result = asyncio.run(registry.execute("datetime", {}))
    upper = datetime.now()
    check("默认返回 datetime 格式（YYYY-MM-DD HH:MM:SS）",
          bool(DATETIME_PATTERN.match(default_result)),
          f"got {default_result!r}")
    low, high = window(lower, upper, "%Y-%m-%d %H:%M:%S")
    check("默认结果落在当前时间窗口内", low <= default_result <= high,
          f"got {default_result!r} not in [{low}, {high}]")

    # format=date / format=time
    lower = datetime.now()
    date_result = asyncio.run(registry.execute("datetime", {"format": "date"}))
    upper = datetime.now()
    check("format=date 返回 YYYY-MM-DD", bool(DATE_PATTERN.match(date_result)),
          f"got {date_result!r}")
    low, high = window(lower, upper, "%Y-%m-%d")
    check("format=date 结果落在当前日期窗口内", low <= date_result <= high,
          f"got {date_result!r} not in [{low}, {high}]")

    lower = datetime.now()
    time_result = asyncio.run(registry.execute("datetime", {"format": "time"}))
    upper = datetime.now()
    check("format=time 返回 HH:MM:SS", bool(TIME_PATTERN.match(time_result)),
          f"got {time_result!r}")
    low, high = window(lower, upper, "%H:%M:%S")
    check("format=time 结果落在当前时间窗口内", low <= time_result <= high,
          f"got {time_result!r} not in [{low}, {high}]")

    # offset_days：昨天 / 明天
    lower = datetime.now()
    tomorrow = asyncio.run(
        registry.execute("datetime", {"format": "date", "offset_days": 1})
    )
    upper = datetime.now()
    low, high = window(lower, upper, "%Y-%m-%d", offset_days=1)
    check("offset_days=1 返回明天", low <= tomorrow <= high,
          f"got {tomorrow!r} not in [{low}, {high}]")

    lower = datetime.now()
    yesterday = asyncio.run(
        registry.execute("datetime", {"format": "date", "offset_days": -1})
    )
    upper = datetime.now()
    low, high = window(lower, upper, "%Y-%m-%d", offset_days=-1)
    check("offset_days=-1 返回昨天", low <= yesterday <= high,
          f"got {yesterday!r} not in [{low}, {high}]")

    # 参数错误 —— 由 Registry 在执行前统一拦截
    message = expect_parameter_error(registry, "datetime", {"format": 5})
    check("format 非字符串 → ToolParameterError",
          message is not None and "类型错误" in message, f"msg={message}")

    message = expect_parameter_error(registry, "datetime", {"format": "year"})
    check("format 非法枚举 → ToolParameterError",
          message is not None and "取值非法" in message, f"msg={message}")

    message = expect_parameter_error(registry, "datetime", {"offset_days": "1"})
    check("offset_days 传字符串 → ToolParameterError",
          message is not None and "类型错误" in message, f"msg={message}")

    message = expect_parameter_error(registry, "datetime", {"offset_days": True})
    check("offset_days 传布尔（bool 不算 integer）→ ToolParameterError",
          message is not None and "类型错误" in message, f"msg={message}")

    message = expect_parameter_error(registry, "datetime", {"unknown": 1})
    check("datetime 传未定义参数 → ToolParameterError",
          message is not None and "不支持未定义参数" in message,
          f"msg={message}")

    # 参数语义错误 —— 由 Tool 自身校验（format=time 不接受 offset_days）
    raised = False
    try:
        asyncio.run(
            registry.execute("datetime", {"format": "time", "offset_days": 2})
        )
    except ToolExecutionError:
        raised = True
    check("format=time 与 offset_days 组合 → ToolExecutionError", raised)


# ----------------------------------------------------------------------
# 3. 参数校验（Schema 驱动，Registry 统一入口）
# ----------------------------------------------------------------------
def test_parameter_validation() -> None:
    print("\n[3] 参数校验（Schema 驱动，Registry 统一入口）")
    registry = build_default_registry()

    # 3.1 正常参数
    check("正常参数通过（calculator）",
          asyncio.run(registry.execute("calculator", {"expression": "2 + 3"}))
          == "5")
    check("正常参数通过（datetime，参数全部可选）",
          bool(DATETIME_PATTERN.match(
              asyncio.run(registry.execute("datetime", {})))))

    # 3.2 缺少必填参数
    message = expect_parameter_error(registry, "calculator", {})
    check("缺少必填参数 → ToolParameterError",
          message is not None
          and "缺少必填参数" in message
          and "'expression'" in message,
          f"msg={message}")

    # 3.3 参数类型错误
    message = expect_parameter_error(registry, "calculator",
                                     {"expression": 123})
    check("参数类型错误 → ToolParameterError",
          message is not None and "类型错误" in message and "string" in message,
          f"msg={message}")

    # 3.4 多余参数（Schema 未定义 → 明确拒绝，不静默忽略）
    message = expect_parameter_error(
        registry,
        "calculator",
        {"expression": "1 + 2", "unknown": "xxx"},
    )
    check("多余参数 → ToolParameterError（不静默忽略）",
          message is not None
          and "不支持未定义参数" in message
          and "'unknown'" in message,
          f"msg={message}")

    # 3.5 不存在的 Tool
    raised = False
    try:
        asyncio.run(registry.execute("not_exists", {}))
    except UnknownToolError as exc:
        raised = True
        check("不存在 Tool 的错误消息列出可用工具",
              "calculator" in exc.message and "datetime" in exc.message,
              f"msg={exc.message}")
    check("不存在 Tool → UnknownToolError", raised)

    # 3.6 arguments 不是 JSON 对象
    message = expect_parameter_error(registry, "calculator", "1 + 2")
    check("参数不是 JSON 对象 → ToolParameterError",
          message is not None and "JSON 对象" in message, f"msg={message}")
    message = expect_parameter_error(registry, "calculator", None)
    check("参数为 None → 按空对象处理（缺少必填参数）",
          message is not None and "缺少必填参数" in message, f"msg={message}")

    # 3.7 校验失败时 Tool.execute() 不会被调用
    spy_registry = ToolRegistry(tools=[SpyTool()])
    spy_tool = spy_registry.get("spy")
    expect_parameter_error(spy_registry, "spy", {})
    expect_parameter_error(spy_registry, "spy", {"value": "x"})
    expect_parameter_error(spy_registry, "spy", {"value": 1, "z": 2})
    check("校验失败时 Tool.execute() 从未被调用",
          isinstance(spy_tool, SpyTool) and spy_tool.calls == [],
          f"calls={getattr(spy_tool, 'calls', None)}")
    check("校验通过时才调用 Tool.execute()",
          asyncio.run(spy_registry.execute("spy", {"value": 3})) == "spy:3"
          and isinstance(spy_tool, SpyTool)
          and spy_tool.calls == [{"value": 3}])

    # 3.8 类型矩阵（string / integer / number / boolean / array / object）
    types_registry = ToolRegistry(tools=[TypesProbeTool()])
    valid_cases = [
        {"s": "text"},
        {"i": 3},
        {"n": 3.5},
        {"n": 3},
        {"b": True},
        {"a": [1, 2]},
        {"o": {"k": "v"}},
        {},
    ]
    for case in valid_cases:
        try:
            ok = asyncio.run(
                types_registry.execute("types_probe", case)
            ) == "types-ok"
        except ToolError:
            ok = False
        check(f"合法参数通过: {case}", ok)

    invalid_cases = [
        ({"s": 1}, "string"),
        ({"i": 1.5}, "integer"),
        ({"i": True}, "integer"),
        ({"n": "1"}, "number"),
        ({"b": "true"}, "boolean"),
        ({"a": "x"}, "array"),
        ({"o": [1]}, "object"),
    ]
    for case, expected_type in invalid_cases:
        message = expect_parameter_error(types_registry, "types_probe", case)
        check(f"非法类型被拒绝: {case}",
              message is not None and expected_type in message,
              f"msg={message}")

    # 3.9 additionalProperties=true：由 Tool Schema 显式放开额外参数
    extra_registry = ToolRegistry(tools=[ExtraAllowedTool()])
    check("additionalProperties=true 允许额外参数",
          asyncio.run(extra_registry.execute(
              "extra_allowed", {"text": "hi", "anything": 1})) == "extra:hi")
    message = expect_parameter_error(extra_registry, "extra_allowed",
                                     {"anything": 1})
    check("放开额外参数时仍强制校验必填参数",
          message is not None and "缺少必填参数" in message, f"msg={message}")

    # 3.10 异常体系
    check("ToolParameterError 继承 ToolError",
          issubclass(ToolParameterError, ToolError))
    check("ToolParameterError 与 ToolExecutionError 语义分离",
          not issubclass(ToolParameterError, ToolExecutionError))


# ----------------------------------------------------------------------
# 4. Tool Calling 全流程（Calculator + DateTime，Mock Provider）
# ----------------------------------------------------------------------
class ScriptedProvider(LLMProvider):
    """按脚本依次返回 tool_call 的假 Provider，并记录每轮 messages / tools。"""

    def __init__(
        self, script: list[ToolCall], final_answer: str = "FINAL"
    ) -> None:
        self.script = list(script)
        self.final_answer = final_answer
        self.messages_seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    async def generate(self, messages: list[dict]) -> str:
        return self.final_answer

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.messages_seen.append([dict(message) for message in messages])
        self.tools_seen.append(tools)
        if self.script:
            return LLMResponse(content=None, tool_calls=[self.script.pop(0)])
        return LLMResponse(content=self.final_answer)


def tool_messages(provider: ScriptedProvider, round_index: int) -> list[dict]:
    """取出第 N 轮发给 LLM 的 tool 结果消息。"""
    return [
        message
        for message in provider.messages_seen[round_index]
        if message.get("role") == "tool"
    ]


def test_tool_calling_flow() -> None:
    print("\n[4] Tool Calling 全流程（Calculator）")
    provider = ScriptedProvider(
        script=[
            ToolCall(
                id="call_calc",
                name="calculator",
                arguments={"expression": "123 + 456"},
            )
        ],
        final_answer="123 + 456 = 579。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    reply = asyncio.run(
        runtime.run([{"role": "user", "content": "123 + 456 等于多少？"}])
    )

    check("Calculator Tool Calling → 最终回答来自 LLM 第二轮",
          reply == "123 + 456 = 579。", f"got {reply!r}")
    check("恰好两轮 LLM 调用（tool_call → final answer）",
          len(provider.messages_seen) == 2,
          f"got {len(provider.messages_seen)}")
    check("LLM 同时收到 2 个 Tool Schema（多 Tool 生效）",
          len(provider.tools_seen[0]) == 2, f"got {len(provider.tools_seen[0])}")
    check("Tool Schema 含 calculator 与 datetime",
          [schema["function"]["name"] for schema in provider.tools_seen[0]]
          == ["calculator", "datetime"])

    second_round = provider.messages_seen[1]
    check("第二轮首位仍是 system prompt",
          second_round[0].get("role") == "system")
    check("第二轮包含 assistant.tool_calls（OpenAI 格式）",
          any(message.get("role") == "assistant" and message.get("tool_calls")
              for message in second_round))
    results = tool_messages(provider, 1)
    check("第二轮包含 tool 结果消息", len(results) == 1, f"got {len(results)}")
    check("tool_result 为 579（整数不带 .0）",
          bool(results) and results[0]["content"] == "579",
          f"got {results[0]['content'] if results else None!r}")
    check("tool_call_id 与请求一致",
          bool(results) and results[0]["tool_call_id"] == "call_calc")


def test_datetime_tool_calling() -> None:
    print("\n[4b] Tool Calling 全流程（DateTime，第二个 Tool）")
    provider = ScriptedProvider(
        script=[
            ToolCall(id="call_dt", name="datetime",
                     arguments={"format": "date"}),
        ],
        final_answer="今天是 X 号。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    lower = datetime.now()
    reply = asyncio.run(runtime.run([{"role": "user", "content": "今天几号？"}]))
    upper = datetime.now()

    check("DateTime Tool Calling → 最终回答来自 LLM 第二轮",
          reply == "今天是 X 号。", f"got {reply!r}")
    results = tool_messages(provider, 1)
    check("第二轮包含 tool 结果消息", len(results) == 1, f"got {len(results)}")
    content = results[0]["content"] if results else ""
    check("tool_result 为日期（YYYY-MM-DD）",
          bool(DATE_PATTERN.match(content)), f"got {content!r}")
    low, high = window(lower, upper, "%Y-%m-%d")
    check("tool_result 落在当前日期窗口内", low <= content <= high,
          f"got {content!r}")
    check("tool_call_id 与请求一致",
          bool(results) and results[0]["tool_call_id"] == "call_dt")


def test_two_tools_in_one_run() -> None:
    print("\n[4c] 一次会话先后调用两个不同 Tool（Registry 按 name 路由）")
    provider = ScriptedProvider(
        script=[
            ToolCall(id="c1", name="calculator",
                     arguments={"expression": "2 * 3"}),
            ToolCall(id="c2", name="datetime", arguments={}),
        ],
        final_answer="已完成计算与时间查询。",
    )
    runtime = AgentRuntime(provider, tool_registry=build_default_registry())
    reply = asyncio.run(
        runtime.run([{"role": "user", "content": "先算 2*3，再告诉我当前时间"}])
    )

    check("两个 Tool 依次调用后返回最终回答",
          reply == "已完成计算与时间查询。", f"got {reply!r}")
    check("共 3 轮 LLM 调用（tool_call → tool_call → final answer）",
          len(provider.messages_seen) == 3, f"got {len(provider.messages_seen)}")

    results = tool_messages(provider, 2)
    check("第三轮包含两条 tool 结果（历史累积）",
          len(results) == 2, f"got {len(results)}")
    contents = [message["content"] for message in results]
    check("calculator 结果进入 tool_result", contents[:1] == ["6"],
          f"got {contents}")
    check("datetime 结果进入 tool_result",
          len(contents) == 2 and bool(DATETIME_PATTERN.match(contents[1])),
          f"got {contents}")
    check("两条 tool 结果的 tool_call_id 分别对应两个 Tool",
          [message["tool_call_id"] for message in results] == ["c1", "c2"],
          f"got {[message['tool_call_id'] for message in results]}")


# ----------------------------------------------------------------------
# 5. 错误处理（Runtime 把 Tool 错误作为 tool_result 回传）
# ----------------------------------------------------------------------
ERROR_CASES: list[tuple[str, ToolCall, str]] = [
    (
        "未知 Tool",
        ToolCall(id="e1", name="not_exists", arguments={}),
        "Unknown tool",
    ),
    (
        "缺少必填参数",
        ToolCall(id="e2", name="calculator", arguments={}),
        "缺少必填参数",
    ),
    (
        "参数类型错误",
        ToolCall(id="e3", name="calculator", arguments={"expression": 123}),
        "类型错误",
    ),
    (
        "多余参数",
        ToolCall(
            id="e4",
            name="calculator",
            arguments={"expression": "1 + 2", "unknown": "xxx"},
        ),
        "不支持未定义参数",
    ),
    (
        "枚举取值非法",
        ToolCall(id="e5", name="datetime", arguments={"format": "year"}),
        "取值非法",
    ),
    (
        "Tool 内部执行错误（除零）",
        ToolCall(id="e6", name="calculator", arguments={"expression": "1 / 0"}),
        "除数不能为 0",
    ),
]


def test_error_handling_through_runtime() -> None:
    print("\n[5] 错误处理（Tool 错误作为 tool_result 回传，不破坏会话）")

    for label, call, expected in ERROR_CASES:
        provider = ScriptedProvider(
            script=[call], final_answer="已向用户说明工具调用失败。"
        )
        runtime = AgentRuntime(provider, tool_registry=build_default_registry())
        reply = asyncio.run(
            runtime.run([{"role": "user", "content": "触发工具错误"}])
        )
        results = (
            tool_messages(provider, 1) if len(provider.messages_seen) > 1 else []
        )
        content = results[0]["content"] if results else ""

        check(f"{label}：会话不中断（仍返回最终回答）",
              reply == "已向用户说明工具调用失败。", f"got {reply!r}")
        check(f"{label}：错误以 'Tool error: ' 形式回传",
              content.startswith("Tool error: "), f"got {content!r}")
        check(f"{label}：错误信息含「{expected}」",
              expected in content, f"got {content!r}")


# ----------------------------------------------------------------------
# 6. 安全回归：Calculator 仍拒绝任意 Python 代码
# ----------------------------------------------------------------------
def test_security_regression() -> None:
    print("\n[6] 安全回归（Calculator 不使用 eval / exec）")
    registry = build_default_registry()

    dangerous = [
        '__import__("os")',
        '__import__("os").system("echo hacked")',
        'open("secret.txt").read()',
        'eval("1 + 1")',
        'exec("print(1)")',
        "1 + a",
        "__builtins__",
        "().__class__.__bases__",
        "lambda: 1",
        "2 ** 10",
        "5 % 2",
    ]
    for expression in dangerous:
        blocked = False
        try:
            asyncio.run(
                registry.execute("calculator", {"expression": expression})
            )
        except ToolError:
            blocked = True
        check(f"经 Registry 仍拦截危险表达式: {expression[:30]!r}", blocked)

    source = (TOOLS_DIR / "builtin.py").read_text(encoding="utf-8")
    called_names = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    check("builtin.py 未调用 eval()", "eval" not in called_names,
          f"called={sorted(called_names)}")
    check("builtin.py 未调用 exec()", "exec" not in called_names,
          f"called={sorted(called_names)}")
    check("builtin.py 仍使用 ast 白名单解析", "ast.parse" in source)
    check("DateTimeTool 仅依赖标准库 datetime",
          "from datetime import datetime, timedelta" in source)

    validation_source = (TOOLS_DIR / "validation.py").read_text(
        encoding="utf-8"
    )
    validation_calls = {
        node.func.id
        for node in ast.walk(ast.parse(validation_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    check("validation.py 未调用 eval() / exec()",
          "eval" not in validation_calls and "exec" not in validation_calls,
          f"called={sorted(validation_calls)}")
    validation_imports = imported_modules(validation_source)
    check("参数校验只依赖标准库与项目自身模块（无第三方依赖）",
          validation_imports == {
              "collections.abc", "app.agents.tools.base",
          },
          f"got {sorted(validation_imports)}")


# ----------------------------------------------------------------------
# 7. 架构边界静态检查
# ----------------------------------------------------------------------
def test_architecture_boundaries() -> None:
    print("\n[7] 架构边界静态检查")
    registry_source = (TOOLS_DIR / "registry.py").read_text(encoding="utf-8")
    runtime_source = (BACKEND_DIR / "app" / "agents" / "runtime.py").read_text(
        encoding="utf-8"
    )

    check("registry.py 不 import 具体 Tool 实现",
          "app.agents.tools.builtin" not in imported_modules(registry_source),
          f"imports={sorted(imported_modules(registry_source))}")
    check("registry.py 不出现任何具体 Tool / 参数名（无按工具名分支）",
          not [
              term
              for term in (
                  "CalculatorTool", "DateTimeTool",
                  "calculator", "datetime", "expression", "offset_days",
              )
              if term in registry_source
          ])
    check("registry.py 的依赖只有 Tool 抽象与校验模块",
          imported_modules(registry_source) == {
              "app.agents.tools.base", "app.agents.tools.validation",
          },
          f"imports={sorted(imported_modules(registry_source))}")
    check("runtime.py 不 import 具体 Tool 实现",
          "app.agents.tools.builtin" not in imported_modules(runtime_source)
          and "CalculatorTool" not in runtime_source
          and "DateTimeTool" not in runtime_source,
          f"imports={sorted(imported_modules(runtime_source))}")
    check("runtime.py 只依赖 ToolRegistry / ToolError 抽象",
          "from app.agents.tools.registry import ToolRegistry" in runtime_source
          and "from app.agents.tools.base import ToolError" in runtime_source)
    check("参数校验实现位于 tools/validation.py",
          (TOOLS_DIR / "validation.py").is_file())
    check("build_default_registry 是具体 Tool 的唯一注册点",
          "build_default_registry" in (
              TOOLS_DIR / "__init__.py"
          ).read_text(encoding="utf-8"))


def main() -> None:
    test_multi_tool_registry()
    test_datetime_tool()
    test_parameter_validation()
    test_tool_calling_flow()
    test_datetime_tool_calling()
    test_two_tools_in_one_run()
    test_error_handling_through_runtime()
    test_security_regression()
    test_architecture_boundaries()

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
