"""Agent 执行观察者（Observer）抽象。

为什么需要
----------
一次 Agent 执行包含「多轮 LLM 调用 + 多次 Tool 调用 + 结束原因 + 耗时」，
这些事实在 Stage 8 已经由 :class:`~app.agents.execution.AgentExecution` 表达。
但状态对象本身不会说话：开发者需要的是**在执行过程中的关键节点被通知**，
从而写出日志、度量或（未来）链路追踪。

因此引入这个最小 Observer 抽象：Runtime 在生命周期节点通知它，
它只负责「看」，不参与任何执行决策。

职责边界（重要）
----------------
Observer:

- **只观察**：接收 ``AgentExecution`` / ``ToolExecution`` 做记录；
- **不控制** Runtime（不能改变循环、不能提前结束、不能改 stop_reason）；
- **不执行** Tool、**不调用** LLM；
- **不碰** 数据库 / 事务 / FastAPI / Repository；
- **不改** 执行逻辑：它的异常由 Runtime 侧的
  :func:`app.agents.observers.notify_observers` 兜住，
  绝不会让 Agent 请求失败。

本模块只定义契约（ABC），不依赖任何具体实现，也不依赖 FastAPI /
Database / RAG / 具体 Provider / 具体 Tool。
"""

from abc import ABC, abstractmethod

from app.agents.execution import AgentExecution, ToolExecution

#: 生命周期钩子名（Runtime 通过 ``notify_observers`` 按名分发；
#: 用常量避免字符串拼写错误）
HOOK_ON_START = "on_start"
HOOK_ON_TOOL_CALL = "on_tool_call"
HOOK_ON_FINISH = "on_finish"


class AgentExecutionObserver(ABC):
    """一次 Agent 执行的观察者。

    实现者只需继承本类并实现三个钩子；钩子是 ``async`` 的，
    因为 Runtime 本身是异步的（也便于未来接入异步的日志/度量后端）。

    注意：钩子**不应该**抛出异常来表达「关注点失败」。即使抛了，
    Runtime 也只会记录一条警告并继续执行（见 ``notify_observers``）。

    Attributes:
        name: 观察者标识（用于日志/调试，便于区分多个观察者）。
    """

    #: 观察者名称（子类可覆盖）
    name: str = "observer"

    @abstractmethod
    async def on_start(self, execution: AgentExecution) -> None:
        """一次 Agent 执行开始时调用（早于任何 LLM / Tool 调用）。

        此时 ``execution`` 上只有 ``messages`` / ``max_iterations`` /
        ``started_at`` 有意义：``iterations == 0``、``stop_reason is None``。

        Args:
            execution: 本次执行状态（**只读**，Observer 不应修改）。
        """

    @abstractmethod
    async def on_tool_call(
        self,
        execution: AgentExecution,
        tool_execution: ToolExecution,
    ) -> None:
        """每次 Tool 调用**结束后**调用（成功与失败都会调用）。

        Args:
            execution: 本次执行状态（已包含该次调用对应的 tool_calls /
                tool_results，``iterations`` 已计入当前轮）。
            tool_execution: 该次 Tool 调用记录（含 tool_call_id / 名称 /
                原始参数 / 结果文本，失败可查 :attr:`ToolExecution.failed`）。
        """

    @abstractmethod
    async def on_finish(self, execution: AgentExecution) -> None:
        """一次 Agent 执行**正常结束**时调用。

        此时 ``stop_reason`` / ``final_response`` / ``finished_at`` /
        ``duration_ms`` 均已设置，可安全用于统计。

        说明：Provider 抛出的异常会向上传播（Stage 8 既定边界），
        这种情况下不会触发本钩子——不伪造「正常结束」。

        Args:
            execution: 本次执行状态（**只读**，Observer 不应修改）。
        """


__all__ = [
    "HOOK_ON_FINISH",
    "HOOK_ON_START",
    "HOOK_ON_TOOL_CALL",
    "AgentExecutionObserver",
]
