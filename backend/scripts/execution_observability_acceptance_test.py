"""Stage 9：Execution Observability 验收测试（进程内，不依赖数据库 / 服务器）。

覆盖 Stage 9 目标（Agent 一次执行的**可观测性**）：

1. Observer 契约（抽象接口、钩子名、具体实现、DI 工厂）
2. Execution lifecycle（on_start → on_tool_call* → on_finish 的顺序与时机）
3. Tool Calling：Observer 能看到 calculator 及其 tool_call_id / 参数 / 结果
4. Multiple tool calls（同一轮多个 + 连续两轮均可观察）
5. Tool failure：Observer 仍能拿到执行信息；**Observer 自身失败不影响 Agent 主流程**
6. Max iteration：stop_reason == max_iterations 可被观察
7. Duration：started_at / finished_at / duration_ms（>= 0，包含 LLM 调用耗时）
8. LoggingExecutionObserver 日志内容 + 安全边界（无密钥、参数脱敏、超长截断）
9. 架构边界静态检查（Runtime 不依赖具体 Observer；Observer 不碰 DB / FastAPI / RAG；
   不在 Repository / Model / Alembic 中出现；无被禁组件与新增依赖）

与既有测试的关系
----------------
Stage 8（``agent_runtime_acceptance_test.py``）已验证 Runtime 执行循环本身；
本脚本不重复验证循环语义，只验证「观察者能看到什么」以及「观察者不影响执行」。

运行方式（在 backend/ 目录下）::

    python -m scripts.execution_observability_acceptance_test

本脚本不读取 / 打印 backend/.env 中的任何敏感值（对 LLM_API_KEY / SECRET_KEY
只做「不出现在日志中」的存在性断言，从不输出其内容）。
"""

import ast
import asyncio
import inspect
import logging
import sys
from pathlib import Path

from app.agents.execution import (
    STOP_REASON_FINAL_ANSWER,
    STOP_REASON_MAX_ITERATIONS,
    TOOL_ERROR_PREFIX,
    AgentExecution,
    ToolExecution,
)
from app.agents.observers import (
    HOOK_ON_FINISH,
    HOOK_ON_START,
    HOOK_ON_TOOL_CALL,
    MASKED_VALUE,
    AgentExecutionObserver,
    LoggingExecutionObserver,
    build_default_observers,
    notify_observers,
    sanitize_arguments,
)
from app.agents.observers.logging import logger as observer_logger
from app.agents.providers.base import LLMProvider, LLMResponse, ToolCall
from app.agents.runtime import MAX_TOOL_ITERATIONS, AgentRuntime
from app.agents.tools import Tool, ToolRegistry, build_default_registry
from app.core.config import settings
from app.core.dependencies import get_execution_observers

BACKEND_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BACKEND_DIR / "app" / "agents"
OBSERVERS_DIR = AGENTS_DIR / "observers"

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def imported_modules(source: str) -> set[str]:
    """用 AST 提取源码中 import 的模块名（不受注释 / 文档字符串干扰）。"""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def snapshot(execution: AgentExecution) -> dict:
    """在钩子触发的那一刻对执行状态做快照。

    必须快照：``AgentExecution`` 是可变对象，钩子拿到的引用在执行结束后
    状态已被更新（否则无法断言「on_start 时 iterations 还是 0」）。
    """
    return {
        "iterations": execution.iterations,
        "tool_calls": len(execution.tool_calls),
        "tool_results": len(execution.tool_results),
        "failed_tool_calls": execution.failed_tool_calls,
        "used_tools": execution.used_tools,
        "stop_reason": execution.stop_reason,
        "final_response": execution.final_response,
        "reached_iteration_limit": execution.reached_iteration_limit,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "duration_ms": execution.duration_ms,
    }


# ----------------------------------------------------------------------
# 测试用 Observer
# ----------------------------------------------------------------------
class RecordingObserver(AgentExecutionObserver):
    """记录所有生命周期事件的观察者（带状态快照，供断言使用）。"""

    name = "recording"

    def __init__(self) -> None:
        self.order: list[str] = []
        self.execution_ids: list[int] = []
        self.started: list[dict] = []
        self.tool_calls: list[ToolExecution] = []
        self.tool_call_snapshots: list[dict] = []
        self.finished: list[dict] = []

    async def on_start(self, execution: AgentExecution) -> None:
        self.order.append(HOOK_ON_START)
        self.execution_ids.append(id(execution))
        self.started.append(snapshot(execution))

    async def on_tool_call(
        self,
        execution: AgentExecution,
        tool_execution: ToolExecution,
    ) -> None:
        self.order.append(HOOK_ON_TOOL_CALL)
        self.execution_ids.append(id(execution))
        self.tool_calls.append(tool_execution)
        self.tool_call_snapshots.append(snapshot(execution))

    async def on_finish(self, execution: AgentExecution) -> None:
        self.order.append(HOOK_ON_FINISH)
        self.execution_ids.append(id(execution))
        self.finished.append(snapshot(execution))


class ExplodingObserver(AgentExecutionObserver):
    """每个钩子都抛异常的观察者（验证「观察者失败不影响主流程」）。"""

    name = "exploding"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_start(self, execution: AgentExecution) -> None:
        self.calls.append(HOOK_ON_START)
        raise RuntimeError("observer on_start 失败")

    async def on_tool_call(
        self,
        execution: AgentExecution,
        tool_execution: ToolExecution,
    ) -> None:
        self.calls.append(HOOK_ON_TOOL_CALL)
        raise RuntimeError("observer on_tool_call 失败")

    async def on_finish(self, execution: AgentExecution) -> None:
        self.calls.append(HOOK_ON_FINISH)
        raise RuntimeError("observer on_finish 失败")


class CancelObserver(AgentExecutionObserver):
    """钩子抛 asyncio.CancelledError（任务取消必须向上传播，不被吞掉）。"""

    name = "cancel"

    async def on_start(self, execution: AgentExecution) -> None:
        raise asyncio.CancelledError()

    async def on_tool_call(
        self,
        execution: AgentExecution,
        tool_execution: ToolExecution,
    ) -> None:
        raise asyncio.CancelledError()

    async def on_finish(self, execution: AgentExecution) -> None:
        raise asyncio.CancelledError()


class CollectingHandler(logging.Handler):
    """收集日志记录（用于断言日志内容，不影响其它 handler）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


# ----------------------------------------------------------------------
# 测试用 Provider / Tool（不接触真实网络与数据库）
# ----------------------------------------------------------------------
class ScriptedProvider(LLMProvider):
    """按脚本依次返回 tool_calls；脚本用完后返回最终回答。"""

    def __init__(
        self,
        script: list[ToolCall] | None = None,
        final_answer: str = "FINAL",
    ) -> None:
        self.script = list(script or [])
        self.final_answer = final_answer
        self.messages_seen: list[list[dict]] = []
        self.generate_calls = 0

    async def generate(self, messages: list[dict]) -> str:
        self.generate_calls += 1
        return self.final_answer

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.messages_seen.append(list(messages))
        if self.script:
            return LLMResponse(content=None, tool_calls=[self.script.pop(0)])
        return LLMResponse(content=self.final_answer)


class TwoCallsProvider(LLMProvider):
    """第 1 轮一次返回两个 tool_calls，第 2 轮给出最终回答。"""

    def __init__(
        self,
        calls: list[ToolCall],
        final_answer: str = "两个都完成了。",
    ) -> None:
        self.calls = list(calls)
        self.final_answer = final_answer
        self.rounds = 0

    async def generate(self, messages: list[dict]) -> str:
        return self.final_answer

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.rounds += 1
        if self.rounds == 1:
            return LLMResponse(content=None, tool_calls=list(self.calls))
        return LLMResponse(content=self.final_answer)


class AlwaysToolProvider(LLMProvider):
    """每轮都要求调用 Tool（永不给出最终回答），用于验证 max_iterations。"""

    def __init__(self, tool_name: str = "calculator") -> None:
        self.tool_name = tool_name
        self.rounds = 0

    async def generate(self, messages: list[dict]) -> str:
        return "兜底最终回答"

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.rounds += 1
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"obs_loop_{self.rounds}",
                    name=self.tool_name,
                    arguments={"expression": "1 + 1"},
                )
            ],
        )


class ExplodingProvider(LLMProvider):
    """``generate_with_tools`` 抛异常（模拟 provider / 网络故障）。"""

    async def generate(self, messages: list[dict]) -> str:
        return "不应到达这里"

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        raise RuntimeError("provider 网络故障")


class SlowProvider(LLMProvider):
    """延迟返回文本（用于验证耗时统计覆盖 Provider 调用）。"""

    def __init__(self, delay_seconds: float, final_answer: str = "慢回答") -> None:
        self.delay_seconds = delay_seconds
        self.final_answer = final_answer

    async def generate(self, messages: list[dict]) -> str:
        await asyncio.sleep(self.delay_seconds)
        return self.final_answer


class EchoTool(Tool):
    """回显参数的工具（允许任意参数，用于日志脱敏测试）。"""

    name = "echo"
    description = "回显参数（Observability 测试用）"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": [],
        "additionalProperties": True,
    }

    async def execute(self, arguments: dict) -> str:
        return f"echo:{arguments.get('text', '')}"


# ----------------------------------------------------------------------
# 1. Observer 契约
# ----------------------------------------------------------------------
def test_observer_interface() -> None:
    print("\n[1] Observer 契约（抽象接口 / 钩子 / 具体实现 / DI 工厂）")

    check("AgentExecutionObserver 是抽象类",
          inspect.isabstract(AgentExecutionObserver))
    raised = False
    try:
        AgentExecutionObserver()  # type: ignore[abstract]
    except TypeError:
        raised = True
    check("抽象 Observer 不能直接实例化", raised)

    check("三个钩子均为 async 方法",
          all(
              inspect.iscoroutinefunction(getattr(AgentExecutionObserver, hook))
              for hook in (HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH)
          ))
    check("钩子名常量稳定",
          (HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH)
          == ("on_start", "on_tool_call", "on_finish"))

    observer = LoggingExecutionObserver()
    check("LoggingExecutionObserver 实现 Observer 抽象",
          isinstance(observer, AgentExecutionObserver)
          and issubclass(LoggingExecutionObserver, AgentExecutionObserver))
    check("LoggingExecutionObserver 有稳定名称",
          observer.name == "logging", f"got {observer.name!r}")
    check("自定义 Observer 可实现全部钩子",
          isinstance(RecordingObserver(), AgentExecutionObserver))

    defaults_a = build_default_observers()
    defaults_b = build_default_observers()
    check("build_default_observers 返回 Observer 列表",
          isinstance(defaults_a, list) and len(defaults_a) >= 1
          and all(isinstance(item, AgentExecutionObserver)
                  for item in defaults_a))
    check("默认观察者不是全局单例",
          defaults_a[0] is not defaults_b[0])

    injected = get_execution_observers()
    check("DI 工厂 get_execution_observers 返回 Observer 列表",
          isinstance(injected, list) and len(injected) >= 1
          and all(isinstance(item, AgentExecutionObserver)
                  for item in injected),
          f"got {injected!r}")
    check("DI 工厂每次新建实例（无全局 mutable state）",
          injected[0] is not get_execution_observers()[0])


# ----------------------------------------------------------------------
# 2. Execution lifecycle
# ----------------------------------------------------------------------
def test_execution_lifecycle() -> None:
    print("\n[2] Execution lifecycle（on_start → on_tool_call* → on_finish）")
    observer = RecordingObserver()
    provider = ScriptedProvider(
        script=[
            ToolCall(id="life_1", name="calculator",
                     arguments={"expression": "1 + 1"})
        ],
        final_answer="1 + 1 = 2。",
    )
    runtime = AgentRuntime(
        provider, tool_registry=build_default_registry(), observers=[observer]
    )
    execution = asyncio.run(
        runtime.execute([{"role": "user", "content": "1+1"}])
    )

    check("事件顺序为 on_start → on_tool_call → on_finish",
          observer.order == [HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH],
          f"got {observer.order}")
    check("三个钩子观察到的是同一个 AgentExecution 对象",
          len(set(observer.execution_ids)) == 1
          and observer.execution_ids[0] == id(execution))
    check("on_start 时状态为空（iterations / stop_reason / duration）",
          observer.started[0]["iterations"] == 0
          and observer.started[0]["stop_reason"] is None
          and observer.started[0]["duration_ms"] is None
          and observer.started[0]["tool_results"] == 0)
    check("on_start 时已写入 started_at",
          observer.started[0]["started_at"] is not None)
    check("on_tool_call 时该次结果已写入执行状态",
          observer.tool_call_snapshots[0]["tool_results"] == 1
          and observer.tool_call_snapshots[0]["iterations"] == 1)
    check("on_finish 时 stop_reason / final_response / duration 均已就绪",
          observer.finished[0]["stop_reason"] == STOP_REASON_FINAL_ANSWER
          and observer.finished[0]["final_response"] == "1 + 1 = 2。"
          and observer.finished[0]["duration_ms"] is not None)
    check("观察者不影响执行结果",
          execution.final_response == "1 + 1 = 2。"
          and execution.started_at is not None
          and execution.finished_at is not None)

    # run() 路径（对上层仍返回 str）
    run_observer = RecordingObserver()
    reply = asyncio.run(
        AgentRuntime(
            ScriptedProvider(final_answer="普通回答"),
            tool_registry=build_default_registry(),
            observers=[run_observer],
        ).run([{"role": "user", "content": "你好"}])
    )
    check("run() 路径同样触发 on_start / on_finish（无 Tool 调用）",
          run_observer.order == [HOOK_ON_START, HOOK_ON_FINISH]
          and reply == "普通回答",
          f"got {run_observer.order}")

    # 无 Tool 路径（registry=None）
    plain_observer = RecordingObserver()
    asyncio.run(
        AgentRuntime(
            ScriptedProvider(final_answer="无工具回答"),
            tool_registry=None,
            observers=[plain_observer],
        ).run([{"role": "user", "content": "你好"}])
    )
    check("无 Tool 的普通对话路径也触发 on_start / on_finish",
          plain_observer.order == [HOOK_ON_START, HOOK_ON_FINISH])

    # 未注入观察者：行为与 Stage 8 完全一致
    execution_no_observer = asyncio.run(
        AgentRuntime(
            ScriptedProvider(final_answer="x"),
            tool_registry=build_default_registry(),
        ).execute([{"role": "user", "content": "hi"}])
    )
    check("未注入观察者时行为不变（零开销路径）",
          execution_no_observer.final_response == "x"
          and execution_no_observer.stop_reason == STOP_REASON_FINAL_ANSWER
          and execution_no_observer.duration_ms is not None)

    # Provider 异常：不触发 on_finish（不伪造正常结束）
    fail_observer = RecordingObserver()
    error: RuntimeError | None = None
    try:
        asyncio.run(
            AgentRuntime(
                ExplodingProvider(),
                tool_registry=build_default_registry(),
                observers=[fail_observer],
            ).run([{"role": "user", "content": "hi"}])
        )
    except RuntimeError as exc:
        error = exc
    check("Provider 异常仍向上传播（Stage 8 边界不变）",
          isinstance(error, RuntimeError)
          and "provider 网络故障" in str(error),
          f"got {error!r}")
    check("Provider 异常时只触发 on_start（不触发 on_finish）",
          fail_observer.order == [HOOK_ON_START],
          f"got {fail_observer.order}")


# ----------------------------------------------------------------------
# 3. 单次 Tool Calling 可观察
# ----------------------------------------------------------------------
def test_single_tool_call_observed() -> None:
    print("\n[3] Tool Calling：Observer 能看到 calculator")
    observer = RecordingObserver()
    provider = ScriptedProvider(
        script=[
            ToolCall(id="obs_calc_1", name="calculator",
                     arguments={"expression": "25 * 4 + 10"})
        ],
        final_answer="25 * 4 + 10 = 110。",
    )
    execution = asyncio.run(
        AgentRuntime(
            provider,
            tool_registry=build_default_registry(),
            observers=[observer],
        ).execute([{"role": "user", "content": "计算 25 * 4 + 10"}])
    )

    check("Observer 观察到 1 次 Tool 调用", len(observer.tool_calls) == 1)
    record = observer.tool_calls[0]
    check("观察到的是 ToolExecution 记录", isinstance(record, ToolExecution))
    check("观察到 tool name = calculator",
          record.name == "calculator", f"got {record.name!r}")
    check("观察到 tool_call_id",
          record.tool_call_id == "obs_calc_1", f"got {record.tool_call_id!r}")
    check("观察到 LLM 原始参数",
          record.arguments == {"expression": "25 * 4 + 10"},
          f"got {record.arguments!r}")
    check("观察到 Tool 成功（failed=False）", record.failed is False)
    check("观察到 tool result 文本", record.result == "110",
          f"got {record.result!r}")
    check("on_finish 汇总 iterations=1 / tool_calls=1 / used_tools=True",
          observer.finished[0]["iterations"] == 1
          and observer.finished[0]["tool_calls"] == 1
          and observer.finished[0]["used_tools"] is True)
    check("观察者不改变最终回答",
          execution.final_response == "25 * 4 + 10 = 110。",
          f"got {execution.final_response!r}")


# ----------------------------------------------------------------------
# 4. Multiple tool calls
# ----------------------------------------------------------------------
def test_multiple_tool_calls_observed() -> None:
    print("\n[4] Multiple tool calls（同轮多个 + 连续两轮）")

    # 4a 同一轮两个 tool_calls
    multi_observer = RecordingObserver()
    multi_provider = TwoCallsProvider(
        calls=[
            ToolCall(id="m1", name="calculator",
                     arguments={"expression": "1 + 1"}),
            ToolCall(id="m2", name="calculator",
                     arguments={"expression": "2 * 3"}),
        ]
    )
    asyncio.run(
        AgentRuntime(
            multi_provider,
            tool_registry=build_default_registry(),
            observers=[multi_observer],
        ).execute([{"role": "user", "content": "算两个式子"}])
    )
    check("同轮两个 Tool 触发两次 on_tool_call",
          multi_observer.order
          == [HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_TOOL_CALL,
              HOOK_ON_FINISH],
          f"got {multi_observer.order}")
    check("两次调用的 tool_call_id 可分别观察",
          [record.tool_call_id for record in multi_observer.tool_calls]
          == ["m1", "m2"],
          f"got {[r.tool_call_id for r in multi_observer.tool_calls]}")
    check("两次调用的结果可分别观察",
          [record.result for record in multi_observer.tool_calls]
          == ["2", "6"],
          f"got {[r.result for r in multi_observer.tool_calls]}")

    # 4b 连续两轮（两个不同 Tool）
    chain_observer = RecordingObserver()
    chain_provider = ScriptedProvider(
        script=[
            ToolCall(id="chain_1", name="calculator",
                     arguments={"expression": "3 + 4"}),
            ToolCall(id="chain_2", name="datetime",
                     arguments={"format": "date"}),
        ],
        final_answer="先算完再取日期，完成。",
    )
    execution = asyncio.run(
        AgentRuntime(
            chain_provider,
            tool_registry=build_default_registry(),
            observers=[chain_observer],
        ).execute([{"role": "user", "content": "先算 3+4，再告诉我今天几号"}])
    )
    check("连续两轮触发两次 on_tool_call（顺序确定）",
          chain_observer.order
          == [HOOK_ON_START] + [HOOK_ON_TOOL_CALL] * 2 + [HOOK_ON_FINISH],
          f"got {chain_observer.order}")
    check("观察到两个不同 Tool（calculator → datetime）",
          [record.name for record in chain_observer.tool_calls]
          == ["calculator", "datetime"],
          f"got {[r.name for r in chain_observer.tool_calls]}")
    check("每次 on_tool_call 的 iterations 递增（1 → 2）",
          [item["iterations"] for item in chain_observer.tool_call_snapshots]
          == [1, 2],
          f"got {[i['iterations'] for i in chain_observer.tool_call_snapshots]}")
    check("on_finish 汇总 iterations=2 / tool_calls=2",
          chain_observer.finished[0]["iterations"] == 2
          and chain_observer.finished[0]["tool_calls"] == 2)
    check("观察者不改变最终回答",
          execution.final_response == "先算完再取日期，完成。",
          f"got {execution.final_response!r}")


# ----------------------------------------------------------------------
# 5. Tool failure 与 Observer 失败隔离
# ----------------------------------------------------------------------
def test_tool_failure_and_observer_isolation() -> None:
    print("\n[5] Tool failure 与 Observer 失败隔离")

    # 5a Tool 失败：Observer 仍能拿到完整执行信息
    cases = [
        (
            "未知 Tool",
            ToolCall(id="f_unknown", name="no_such_tool", arguments={}),
            "Unknown tool",
        ),
        (
            "参数校验失败",
            ToolCall(id="f_param", name="calculator", arguments={}),
            "缺少必填参数",
        ),
        (
            "Tool 执行失败",
            ToolCall(id="f_exec", name="calculator",
                     arguments={"expression": "1 / 0"}),
            "除数不能为 0",
        ),
    ]
    for label, call, expected in cases:
        observer = RecordingObserver()
        provider = ScriptedProvider(
            script=[call], final_answer="已向用户说明工具失败。"
        )
        execution = asyncio.run(
            AgentRuntime(
                provider,
                tool_registry=build_default_registry(),
                observers=[observer],
            ).execute([{"role": "user", "content": "触发工具失败"}])
        )
        record = observer.tool_calls[0]

        check(f"{label}：Observer 仍收到 on_tool_call",
              len(observer.tool_calls) == 1)
        check(f"{label}：Observer 可判断该次调用失败",
              record.failed is True
              and record.result.startswith(TOOL_ERROR_PREFIX),
              f"got {record.result!r}")
        check(f"{label}：Observer 收到失败原因「{expected}」",
              expected in record.result, f"got {record.result!r}")
        check(f"{label}：on_finish 正常触发并汇总失败次数",
              observer.order
              == [HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH]
              and observer.finished[0]["failed_tool_calls"] == 1
              and observer.finished[0]["stop_reason"]
              == STOP_REASON_FINAL_ANSWER)
        check(f"{label}：Agent 执行结果不受观察影响",
              execution.final_response == "已向用户说明工具失败。")

    # 5b Observer 全部钩子抛异常：Agent 主流程与其它 Observer 均不受影响
    exploding = ExplodingObserver()
    recording = RecordingObserver()
    observer_pkg_logger = logging.getLogger("app.agents.observers")
    handler = CollectingHandler()
    handler.setLevel(logging.WARNING)
    previous_level = observer_pkg_logger.level
    observer_pkg_logger.addHandler(handler)
    observer_pkg_logger.setLevel(logging.WARNING)
    try:
        provider = ScriptedProvider(
            script=[
                ToolCall(id="iso_1", name="calculator",
                         arguments={"expression": "2 + 2"})
            ],
            final_answer="2 + 2 = 4。",
        )
        execution = asyncio.run(
            AgentRuntime(
                provider,
                tool_registry=build_default_registry(),
                observers=[exploding, recording],
            ).execute([{"role": "user", "content": "2+2"}])
        )
    finally:
        observer_pkg_logger.removeHandler(handler)
        observer_pkg_logger.setLevel(previous_level)

    check("Observer 异常不影响 Agent 最终回答",
          execution.final_response == "2 + 2 = 4。",
          f"got {execution.final_response!r}")
    check("Observer 异常不影响执行状态",
          execution.iterations == 1
          and execution.stop_reason == STOP_REASON_FINAL_ANSWER
          and execution.failed_tool_calls == 0)
    check("失败的 Observer 仍被完整通知（3 个钩子都调用过）",
          exploding.calls == [HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH],
          f"got {exploding.calls}")
    check("失败的 Observer 不阻断后续 Observer",
          recording.order
          == [HOOK_ON_START, HOOK_ON_TOOL_CALL, HOOK_ON_FINISH],
          f"got {recording.order}")
    check("Observer 异常被记录为 warning 日志（3 条）",
          len(handler.records) == 3
          and all(record.levelno == logging.WARNING
                  for record in handler.records),
          f"got {[r.levelname for r in handler.records]}")
    check("warning 日志标明观察者 / 钩子 / 异常类型",
          all("observer hook failed" in message
              and "exploding" in message
              and "RuntimeError" in message
              for message in handler.messages),
          f"got {handler.messages[:1]}")

    # 5c notify_observers 的边界行为
    stub = AgentExecution(messages=[], max_iterations=1)
    asyncio.run(notify_observers(None, HOOK_ON_START, stub))
    asyncio.run(notify_observers([], HOOK_ON_START, stub))
    check("observers 为 None / 空列表时安全无操作", True)

    class NoHooks:
        """没有任何钩子的对象（防御性分支）。"""

    asyncio.run(
        notify_observers(
            [NoHooks()],  # type: ignore[list-item]
            HOOK_ON_TOOL_CALL,
            stub,
            ToolExecution(tool_call_id="x", name="n", arguments={},
                          result="ok"),
        )
    )
    check("缺少钩子的对象被安全跳过", True)

    cancel_error: BaseException | None = None
    try:
        asyncio.run(notify_observers([CancelObserver()], HOOK_ON_START, stub))
    except BaseException as exc:  # noqa: BLE001 - 仅判断是否向上传播
        cancel_error = exc
    check("CancelledError 不被当作 Observer 异常吞掉",
          isinstance(cancel_error, asyncio.CancelledError),
          f"got {cancel_error!r}")


# ----------------------------------------------------------------------
# 6. Max iteration 可观察
# ----------------------------------------------------------------------
def test_max_iterations_observed() -> None:
    print("\n[6] Max iteration（stop_reason == max_iterations 可被观察）")
    check("默认上限仍为 3（Stage 8 行为不变）", MAX_TOOL_ITERATIONS == 3)

    observer = RecordingObserver()
    execution = asyncio.run(
        AgentRuntime(
            AlwaysToolProvider(),
            tool_registry=build_default_registry(),
            observers=[observer],
            max_iterations=2,
        ).execute([{"role": "user", "content": "一直调用工具"}])
    )
    check("on_tool_call 次数等于 max_iterations",
          observer.order
          == [HOOK_ON_START] + [HOOK_ON_TOOL_CALL] * 2 + [HOOK_ON_FINISH],
          f"got {observer.order}")
    check("每个 tool_call 都被观察到（calculator ×2）",
          [record.name for record in observer.tool_calls]
          == ["calculator", "calculator"])
    check("on_finish 可观察 stop_reason == max_iterations",
          observer.finished[0]["stop_reason"] == STOP_REASON_MAX_ITERATIONS,
          f"got {observer.finished[0]['stop_reason']!r}")
    check("on_finish 可观察 reached_iteration_limit == True",
          observer.finished[0]["reached_iteration_limit"] is True)
    check("on_finish 的 iterations 等于上限",
          observer.finished[0]["iterations"] == 2)
    check("达到上限仍返回最终回答（观察者不改变兜底逻辑）",
          execution.final_response == "兜底最终回答"
          and execution.reached_iteration_limit is True,
          f"got {execution.final_response!r}")

    default_observer = RecordingObserver()
    default_execution = asyncio.run(
        AgentRuntime(
            AlwaysToolProvider(),
            tool_registry=build_default_registry(),
            observers=[default_observer],
        ).execute([{"role": "user", "content": "一直调用"}])
    )
    check("默认 max_iterations=3 → 可观察 3 次 Tool 调用",
          default_observer.order.count(HOOK_ON_TOOL_CALL) == 3,
          f"got {default_observer.order}")
    check("默认上限下 stop_reason 同样可观察",
          default_observer.finished[0]["stop_reason"]
          == STOP_REASON_MAX_ITERATIONS
          and default_execution.iterations == MAX_TOOL_ITERATIONS)


# ----------------------------------------------------------------------
# 7. Duration
# ----------------------------------------------------------------------
def test_duration() -> None:
    print("\n[7] Duration（started_at / finished_at / duration_ms）")
    fresh = AgentExecution(messages=[], max_iterations=1)
    check("未执行时无耗时信息",
          fresh.started_at is None and fresh.finished_at is None
          and fresh.duration_ms is None)

    observer = RecordingObserver()
    provider = ScriptedProvider(
        script=[
            ToolCall(id="d1", name="calculator",
                     arguments={"expression": "6 * 7"})
        ],
        final_answer="6 * 7 = 42。",
    )
    execution = asyncio.run(
        AgentRuntime(
            provider,
            tool_registry=build_default_registry(),
            observers=[observer],
        ).execute([{"role": "user", "content": "6*7"}])
    )

    check("执行后 duration_ms 存在且为 float",
          isinstance(execution.duration_ms, float),
          f"got {execution.duration_ms!r}")
    check("duration_ms >= 0",
          execution.duration_ms is not None and execution.duration_ms >= 0)
    check("finished_at >= started_at",
          execution.started_at is not None
          and execution.finished_at is not None
          and execution.finished_at >= execution.started_at)
    check("duration_ms 与时间戳一致（保留 3 位小数）",
          execution.duration_ms is not None
          and abs(
              execution.duration_ms
              - round(
                  (execution.finished_at - execution.started_at) * 1000, 3
              )
          ) < 1e-6)
    check("Observer 在 on_start 读不到 duration_ms（尚未结束）",
          observer.started[0]["duration_ms"] is None)
    check("Observer 在 on_finish 可读到 duration_ms",
          isinstance(observer.finished[0]["duration_ms"], float)
          and observer.finished[0]["duration_ms"] >= 0)

    # 耗时覆盖 Provider 调用时间
    slow_observer = RecordingObserver()
    slow_execution = asyncio.run(
        AgentRuntime(
            SlowProvider(delay_seconds=0.03),
            tool_registry=build_default_registry(),
            observers=[slow_observer],
        ).execute([{"role": "user", "content": "hi"}])
    )
    check("耗时包含 Provider 调用时间（>= 25ms）",
          slow_execution.duration_ms is not None
          and slow_execution.duration_ms >= 25,
          f"got {slow_execution.duration_ms}")
    check("慢 Provider 的执行也正常结束并汇报 stop_reason",
          slow_observer.finished[0]["stop_reason"] == STOP_REASON_FINAL_ANSWER)

    # mark_* 的独立行为
    manual = AgentExecution(messages=[], max_iterations=1)
    manual.mark_finished()
    check("未 mark_started 时 duration_ms 为 None（不抛异常）",
          manual.duration_ms is None and manual.finished_at is not None)
    manual_ok = AgentExecution(messages=[], max_iterations=1)
    manual_ok.mark_started()
    manual_ok.mark_finished()
    check("手动 mark_started / mark_finished 可得到耗时",
          isinstance(manual_ok.duration_ms, float)
          and manual_ok.duration_ms >= 0,
          f"got {manual_ok.duration_ms!r}")


# ----------------------------------------------------------------------
# 8. LoggingExecutionObserver 内容与安全边界
# ----------------------------------------------------------------------
def test_logging_observer_output() -> None:
    print("\n[8] LoggingExecutionObserver 日志内容与安全边界")
    handler = CollectingHandler()
    handler.setLevel(logging.DEBUG)
    previous_level = observer_logger.level
    observer_logger.addHandler(handler)
    observer_logger.setLevel(logging.DEBUG)
    try:
        provider = ScriptedProvider(
            script=[
                ToolCall(id="log_calc_1", name="calculator",
                         arguments={"expression": "7 * 6"})
            ],
            final_answer="7 * 6 = 42。",
        )
        execution = asyncio.run(
            AgentRuntime(
                provider,
                tool_registry=build_default_registry(),
                observers=[LoggingExecutionObserver()],
            ).execute([{"role": "user", "content": "7*6"}])
        )
    finally:
        observer_logger.removeHandler(handler)
        observer_logger.setLevel(previous_level)

    text = handler.text
    started = [m for m in handler.messages
               if m.startswith("agent execution started")]
    tool_lines = [m for m in handler.messages if "agent tool call" in m]
    finished = [m for m in handler.messages
                if m.startswith("agent execution finished")]
    debug_lines = [m for m in handler.messages
                   if m.startswith("agent tool result")]

    check("记录 execution start（含 max_iterations）",
          len(started) == 1 and "max_iterations=3" in started[0],
          f"got {started[:1]}")
    check("记录 tool call（name / tool_call_id / success / arguments）",
          len(tool_lines) == 1
          and "name=calculator" in tool_lines[0]
          and "tool_call_id=log_calc_1" in tool_lines[0]
          and "success=True" in tool_lines[0]
          and "arguments={'expression': '7 * 6'}" in tool_lines[0],
          f"got {tool_lines[:1]}")
    check("记录 execution finish（轮数 / Tool 统计 / 结束原因 / 上限 / 耗时）",
          len(finished) == 1
          and all(
              token in finished[0]
              for token in (
                  "iterations=1",
                  "tool_calls=1",
                  "used_tools=True",
                  "stop_reason=final_answer",
                  "reached_iteration_limit=False",
                  "duration_ms=",
              )
          ),
          f"got {finished[:1]}")
    check("tool 结果正文只在 DEBUG 级别记录",
          len(debug_lines) == 1 and "result=42" in debug_lines[0],
          f"got {debug_lines[:1]}")
    check("日志条数等于事件数（start + tool info/debug + finish）",
          len(handler.records) == 4,
          f"got {[r.levelname for r in handler.records]}")
    check("日志不影响执行结果", execution.final_response == "7 * 6 = 42。")

    # 密钥类信息绝不入日志（仅做存在性断言，不输出其值）
    api_key = settings.LLM_API_KEY
    check("日志不含 LLM_API_KEY", (not api_key) or (api_key not in text))
    check("日志不含 SECRET_KEY", settings.SECRET_KEY not in text)
    check("日志不含 Authorization / Bearer 头",
          "authorization" not in text.lower()
          and "bearer " not in text.lower())
    check("日志不含数据库连接串",
          "postgresql" not in text.lower() and "asyncpg" not in text.lower())


# ----------------------------------------------------------------------
# 8b. sanitize_arguments 脱敏边界
# ----------------------------------------------------------------------
def test_sanitize_arguments() -> None:
    print("\n[8b] sanitize_arguments（最小脱敏 / 截断边界）")
    arguments = {
        "expression": "1 + 1",
        "api_key": "sk-stage9-fake-key",
        "Password": "p@ssw0rd",
        "nested_token": "tok",
        "count": 3,
        "text": "ok",
    }
    sanitized = sanitize_arguments(arguments)

    check("敏感键被脱敏（api_key / Password / nested_token）",
          sanitized["api_key"] == MASKED_VALUE
          and sanitized["Password"] == MASKED_VALUE
          and sanitized["nested_token"] == MASKED_VALUE,
          f"got {sanitized}")
    check("非敏感键保留原值",
          sanitized["expression"] == "1 + 1"
          and sanitized["count"] == 3
          and sanitized["text"] == "ok")
    check("脱敏不修改原始参数字典",
          arguments["api_key"] == "sk-stage9-fake-key")

    long_value = "x" * 500
    truncated = sanitize_arguments({"text": long_value})["text"]
    check("超长字符串被截断（避免日志膨胀）",
          len(truncated) < 500 and "truncated" in truncated,
          f"got len={len(truncated)}")
    check("非 dict 参数原样返回（不猜测结构）",
          sanitize_arguments("raw") == "raw"
          and sanitize_arguments(None) is None
          and sanitize_arguments(123) == 123)

    # 真实执行：带敏感参数的 Tool 调用不把敏感值写进日志
    secret_value = "S3cr3t-Value-For-Stage9-Test"  # 测试用假值
    handler = CollectingHandler()
    handler.setLevel(logging.DEBUG)
    previous_level = observer_logger.level
    observer_logger.addHandler(handler)
    observer_logger.setLevel(logging.DEBUG)
    try:
        provider = ScriptedProvider(
            script=[
                ToolCall(
                    id="sec_1",
                    name="echo",
                    arguments={
                        "text": "hello",
                        "password": secret_value,
                        "api_key": secret_value,
                    },
                )
            ],
            final_answer="done",
        )
        asyncio.run(
            AgentRuntime(
                provider,
                tool_registry=ToolRegistry(tools=[EchoTool()]),
                observers=[LoggingExecutionObserver()],
            ).execute([{"role": "user", "content": "hi"}])
        )
    finally:
        observer_logger.removeHandler(handler)
        observer_logger.setLevel(previous_level)

    secret_text = handler.text
    check("带敏感键的 Tool 参数被脱敏为 ***",
          MASKED_VALUE in secret_text, f"got {secret_text[:120]!r}")
    check("敏感值未出现在日志中", secret_value not in secret_text)
    check("非敏感参数仍可用于调试（便于排查）",
          "hello" in secret_text)


# ----------------------------------------------------------------------
# 9. 架构边界静态检查
# ----------------------------------------------------------------------
def test_architecture_boundaries() -> None:
    print("\n[9] 架构边界静态检查")
    runtime_source = (AGENTS_DIR / "runtime.py").read_text(encoding="utf-8")
    base_source = (OBSERVERS_DIR / "base.py").read_text(encoding="utf-8")
    logging_source = (OBSERVERS_DIR / "logging.py").read_text(encoding="utf-8")
    init_source = (OBSERVERS_DIR / "__init__.py").read_text(encoding="utf-8")
    service_source = (
        BACKEND_DIR / "app" / "services" / "agent.py"
    ).read_text(encoding="utf-8")
    dependencies_source = (
        BACKEND_DIR / "app" / "core" / "dependencies.py"
    ).read_text(encoding="utf-8")

    runtime_imports = imported_modules(runtime_source)
    check("runtime.py 只依赖 Observer 抽象（不含具体实现模块）",
          runtime_imports == {
              "app.agents.execution",
              "app.agents.observers",
              "app.agents.providers",
              "app.agents.providers.base",
              "app.agents.tools.base",
              "app.agents.tools.registry",
          },
          f"got {sorted(runtime_imports)}")
    check("runtime.py 未硬编码具体 Observer / 默认装配",
          "LoggingExecutionObserver" not in runtime_source
          and "build_default_observers" not in runtime_source)
    forbidden = (
        "fastapi", "sqlalchemy", "app.database", "app.rag", "chromadb",
        "openai", "httpx", "requests",
    )
    violations = [
        module for module in runtime_imports
        if any(module == item or module.startswith(item + ".")
               for item in forbidden)
    ]
    check("runtime.py 不依赖 FastAPI / DB / RAG / HTTP SDK",
          not violations, f"{violations}")

    check("observers/base.py 仅依赖 abc + execution（纯契约）",
          imported_modules(base_source) == {"abc", "app.agents.execution"},
          f"got {sorted(imported_modules(base_source))}")
    check("observers/logging.py 仅依赖标准库 logging + 抽象",
          imported_modules(logging_source) == {
              "logging", "app.agents.execution", "app.agents.observers.base",
          },
          f"got {sorted(imported_modules(logging_source))}")
    check("observers/__init__.py 只依赖标准库与包内部",
          imported_modules(init_source) == {
              "logging", "collections.abc", "app.agents.execution",
              "app.agents.observers.base", "app.agents.observers.logging",
          },
          f"got {sorted(imported_modules(init_source))}")
    observer_modules = set().union(*(
        imported_modules(source)
        for source in (base_source, logging_source, init_source)
    ))
    check("observers 不导入项目配置 / 不读取环境变量（不触碰 secret）",
          "app.core.config" not in observer_modules
          and "os" not in observer_modules
          and "settings." not in logging_source
          and "os.environ" not in logging_source,
          f"imports={sorted(observer_modules)}")

    observer_imports = observer_modules
    banned_packages = (
        "langchain", "langgraph", "mcp", "redis", "celery", "kafka",
        "opentelemetry", "prometheus", "elasticsearch", "anthropic", "openai",
        "fastapi", "sqlalchemy", "app.database", "app.rag", "chromadb",
    )
    check("observers 包不依赖被禁组件 / DB / FastAPI / RAG",
          not [
              module for module in observer_imports
              if any(module == item or module.startswith(item + ".")
                     for item in banned_packages)
          ],
          f"{sorted(observer_imports)}")

    # Stage 17：Service 按 Agent 配置选择 Runtime（self.runtime 或按 Agent 构建的
    # runtime），但调用契约不变 —— 仍是 runtime.run(messages, system_prompt=...)
    check("Service 仍以 runtime.run(messages, system_prompt=...) 调用（契约未变）",
          "runtime.run(messages, system_prompt=system_prompt)"
          in service_source)
    check("Service 未引入 Observability 逻辑",
          "observer" not in service_source.lower()
          and "AgentExecution" not in service_source)
    check("DI 以 observers=get_execution_observers() 注入",
          "observers=get_execution_observers()" in dependencies_source
          and "def get_execution_observers(" in dependencies_source)
    # Stage 17：ToolRegistry 仍由 DI 构建并注入 Runtime
    # （构建 Runtime 的代码移入 build_agent_runtime 工厂，注入关系未变）
    check("DI 仍先构建 tool_registry（Stage 6/7 注入未变）",
          "get_tool_registry()" in dependencies_source
          and "tool_registry=registry" in dependencies_source)

    watched = [
        *sorted((BACKEND_DIR / "app" / "models").glob("*.py")),
        *sorted((BACKEND_DIR / "app" / "repositories").glob("*.py")),
        *sorted((BACKEND_DIR / "alembic" / "versions").glob("*.py")),
    ]
    polluted = [
        path.name for path in watched
        if "observer" in path.read_text(encoding="utf-8").lower()
        or "AgentExecution" in path.read_text(encoding="utf-8")
    ]
    check("Repository / Model / Alembic 中无 Observability 代码",
          not polluted, f"{polluted}")

    # Stage 18：改为**动态**校验 migration 状态（不再写死 001–004）。
    # 保持原意：
    #   1) migration 编号必须是 1..N 的连续序列（新增 Stage 不会让断言失效，
    #      但编号断档 / 跳号仍会被发现）；
    #   2) Observability 是纯内存能力，**不得引入任何数据库结构**。
    migrations = sorted(
        path.name
        for path in (BACKEND_DIR / "alembic" / "versions").glob("*.py")
    )
    numbers = [name.split("_", 1)[0] for name in migrations]
    expected_numbers = [
        str(index).zfill(3) for index in range(1, len(numbers) + 1)
    ]
    check("migration 编号连续（动态校验，不写死 001–004）",
          numbers == expected_numbers and len(numbers) >= 4,
          f"got {migrations}")

    observability_migrations = [
        path.name
        for path in (BACKEND_DIR / "alembic" / "versions").glob("*.py")
        if "observer" in path.read_text(encoding="utf-8").lower()
        or "AgentExecution" in path.read_text(encoding="utf-8")
    ]
    check("Observability 未引入数据库结构（migration 中无 observer/execution）",
          not observability_migrations,
          f"{observability_migrations}")

    pyproject = (
        BACKEND_DIR / "pyproject.toml"
    ).read_text(encoding="utf-8").lower()
    banned_deps = [
        name for name in (
            "langchain", "langgraph", "redis", "celery", "opentelemetry",
            "prometheus", "kafka", "elasticsearch", "mcp",
        )
        if name in pyproject
    ]
    check("pyproject.toml 未新增被禁依赖", not banned_deps, f"{banned_deps}")


def main() -> None:
    test_observer_interface()
    test_execution_lifecycle()
    test_single_tool_call_observed()
    test_multiple_tool_calls_observed()
    test_tool_failure_and_observer_isolation()
    test_max_iterations_observed()
    test_duration()
    test_logging_observer_output()
    test_sanitize_arguments()
    test_architecture_boundaries()

    print("\n" + "=" * 56)
    print("Stage 9 Execution Observability acceptance: "
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











