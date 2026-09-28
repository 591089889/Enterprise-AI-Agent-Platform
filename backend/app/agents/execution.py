"""Agent 一次执行的轻量状态表示（Stage 8）。

为什么需要
----------
一次 Agent 执行并不只是一次 LLM 调用，而可能是下面这样一段多轮过程::

    LLM → assistant.tool_calls → Tool → tool_result → LLM → ... → final answer

如果这些中间状态只存在于 ``runtime.py`` 的局部变量里，就很难回答
「这次执行到底跑了几轮 Tool」「每个 tool_call 对应哪个结果」
「为什么在这里结束」——测试与排查只能靠间接推断。

因此把执行过程显式表示为一个**轻量状态对象**（:class:`AgentExecution`），
由 ``AgentRuntime`` 在循环中原地更新，供上层与测试直接观察。

设计说明
--------
- 只用标准库 ``dataclasses`` 表达状态，**不引入**状态机框架 / 事件总线 /
  工作流引擎 / 队列 / 数据库状态表。
- 只有数据与极少量只读属性，**不含流程控制逻辑**：循环与错误边界仍然只在
  ``AgentRuntime`` 中，避免同一套流程分散到两个地方。
- 与具体实现解耦：仅依赖 :class:`~app.agents.providers.base.ToolCall`
  这一数据结构，不依赖任何具体 Provider / Tool / 数据库。
- 执行耗时（Stage 9）用标准库 ``time.perf_counter()`` 在**内存内**统计
  （单调时钟，只用于度量），不落库、不做持久化、不是分布式链路追踪。
"""

from dataclasses import dataclass, field
from time import perf_counter

from app.agents.providers.base import ToolCall

#: Tool 执行失败时 tool_result 的统一前缀（Runtime 回传给 LLM 的约定格式）。
#: 测试与上层都依赖该前缀判断「这条 tool 消息是错误说明而非正常结果」。
TOOL_ERROR_PREFIX = "Tool error:"

#: 结束原因：LLM 直接给出了最终回答（该轮没有 tool_calls）。
STOP_REASON_FINAL_ANSWER = "final_answer"

#: 结束原因：达到最大 Tool Calling 轮数，Runtime 改为「不再携带 tools」
#: 索取一次最终回答，保证调用方始终能拿到回答。
STOP_REASON_MAX_ITERATIONS = "max_iterations"


@dataclass
class ToolExecution:
    """一次 Tool 调用的执行记录（tool_call ↔ tool_result 的一一对应）。

    Attributes:
        tool_call_id: LLM 给出的调用 ID（回传 tool 消息时必须原样使用）。
        name: 工具名称（对应 :attr:`Tool.name`）。
        arguments: LLM 给出的**原始**参数（不被 Runtime 修改，便于核对）。
        result: 回传给 LLM 的结果文本；失败时为 ``Tool error: ...`` 说明。
    """

    tool_call_id: str
    name: str
    arguments: dict
    result: str

    @property
    def failed(self) -> bool:
        """该次调用是否失败（结果为 ``Tool error:`` 说明文本）。"""
        return self.result.startswith(TOOL_ERROR_PREFIX)


@dataclass
class AgentExecution:
    """Agent 一次执行的完整状态。

    Attributes:
        messages: 本次执行的完整消息序列（system + 历史 + 每轮追加的
            ``assistant.tool_calls`` / ``tool`` 消息）。它是 Runtime 的
            **内部副本**，与调用方传入的 ``messages`` 是不同列表。
        max_iterations: 本次执行允许的最大 Tool Calling 轮数。
        iterations: 已完成的 Tool Calling 轮数（即调用 ``generate_with_tools``
            且模型要求调用工具的轮数；普通对话路径为 0）。
        tool_calls: 本次执行中模型要求执行的全部 ToolCall（按时间顺序）。
        tool_results: 与 ``tool_calls`` 一一对应的执行结果。
        final_response: 返回给上层的最终回答文本。
        stop_reason: 结束原因，取值见 :data:`STOP_REASON_FINAL_ANSWER` /
            :data:`STOP_REASON_MAX_ITERATIONS`；未结束时为 ``None``。
        started_at: 执行开始时刻（``time.perf_counter()`` 单调时钟读数，
            仅用于耗时统计，不是墙钟时间）。
        finished_at: 执行结束时刻（同上）；执行未结束时为 ``None``。
        duration_ms: 执行耗时（毫秒，保留 3 位小数）；由
            :meth:`mark_finished` 计算，未结束时为 ``None``。
    """

    messages: list[dict]
    max_iterations: int
    iterations: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolExecution] = field(default_factory=list)
    final_response: str = ""
    stop_reason: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: float | None = None

    @property
    def used_tools(self) -> bool:
        """本次执行是否发生过 Tool Calling。"""
        return bool(self.tool_calls)

    @property
    def reached_iteration_limit(self) -> bool:
        """是否因为达到最大轮数而结束（而非模型给出最终回答）。"""
        return self.stop_reason == STOP_REASON_MAX_ITERATIONS

    @property
    def failed_tool_calls(self) -> int:
        """本次执行中失败的 Tool 调用数量（``Tool error:`` 结果数量）。"""
        return sum(1 for record in self.tool_results if record.failed)

    def mark_started(self) -> None:
        """记录执行开始时刻（由 Runtime 在通知 ``on_start`` 前调用）。

        多次调用会以最后一次为准；只做时间戳记录，不含任何流程控制。
        """
        self.started_at = perf_counter()

    def mark_finished(self) -> None:
        """记录执行结束时刻并计算 :attr:`duration_ms`。

        未调用过 :meth:`mark_started` 时不计算耗时（``duration_ms`` 保持
        ``None``），不会抛异常——观察者/测试可安全调用。
        """
        self.finished_at = perf_counter()
        if self.started_at is not None:
            self.duration_ms = round(
                (self.finished_at - self.started_at) * 1000, 3
            )


__all__ = [
    "STOP_REASON_FINAL_ANSWER",
    "STOP_REASON_MAX_ITERATIONS",
    "TOOL_ERROR_PREFIX",
    "AgentExecution",
    "ToolExecution",
]
