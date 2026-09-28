"""Agent 执行观察者包（Execution Observability，Stage 9）。

调用链::

    AgentRuntime
        ↓  在生命周期节点调用 notify_observers()
    AgentExecutionObserver        # 抽象：on_start / on_tool_call / on_finish
        ↓
    LoggingExecutionObserver      # 具体实现（标准库 logging）

职责边界
--------
- ``base``    ：观察者契约（ABC）+ 钩子名常量，不含任何实现。
- ``logging`` ：把执行生命周期写入标准库 logging（含最小脱敏 / 截断）。
- 本模块      ：默认观察者装配（``build_default_observers``）与
  **安全分发**（``notify_observers``）。

设计说明
--------
- **不是事件总线**：没有订阅/发布、没有队列、没有后台任务，
  只是一个「按顺序逐个 ``await`` 调用钩子」的小函数。
- **Observer 异常绝不影响 Agent 主流程**：``notify_observers`` 逐个
  ``try/except``，失败只记录一条 ``warning`` 日志后继续；
  但``asyncio.CancelledError``（``BaseException``）**不吞**，
  保证任务取消语义与 Stage 8 一致。
- 具体观察者由 DI（``core/dependencies.py``）注入 Runtime，
  Runtime 只依赖 ``AgentExecutionObserver`` 抽象，
  **不在 Runtime 内部硬编码或 new 任何具体 Observer**。

本包不依赖 FastAPI / Database / Repository / RAG；
不引入 LangGraph / LangChain / Event Bus / Redis / Celery / OpenTelemetry。
"""

import logging as stdlib_logging
from collections.abc import Sequence

from app.agents.execution import AgentExecution, ToolExecution
from app.agents.observers.base import (
    HOOK_ON_FINISH,
    HOOK_ON_START,
    HOOK_ON_TOOL_CALL,
    AgentExecutionObserver,
)
from app.agents.observers.logging import (
    MASKED_VALUE,
    MAX_VALUE_LENGTH,
    LoggingExecutionObserver,
    sanitize_arguments,
)

#: 观察者自身失败时的日志记录器（与业务日志分离，避免递归依赖）。
#: 注意：本包内存在同名子模块 ``observers.logging``，它会在包命名空间中
#: 遮蔽标准库 ``logging``，因此这里用 ``stdlib_logging`` 别名显式引用标准库。
_observer_logger = stdlib_logging.getLogger(__name__)


def build_default_observers() -> list[AgentExecutionObserver]:
    """构建默认观察者列表（当前仅 :class:`LoggingExecutionObserver`）。

    与 ``tools.build_default_registry()`` 同一风格：新增观察者时只需在
    本函数追加一行，上层（DI / Runtime）无需改动。

    返回**新列表**（不是全局单例）：不同请求 / 不同 Runtime 之间不共享
    可变状态，避免「全局 mutable state」。

    Returns:
        list[AgentExecutionObserver]: 默认启用的观察者。
    """
    return [LoggingExecutionObserver()]


async def notify_observers(
    observers: Sequence[AgentExecutionObserver] | None,
    hook_name: str,
    execution: AgentExecution,
    tool_execution: ToolExecution | None = None,
) -> None:
    """把一次生命周期事件安全地分发给全部观察者。

    行为约定：

    1. 按传入顺序**逐个 await**（单次 Agent 执行内观察顺序确定，便于测试）；
    2. 某个观察者抛异常 → 记录 ``warning`` 并 **继续通知后面的观察者**
       与 Agent 主流程（永不向上抛出）；
    3. ``observers`` 为空 / ``None`` → 什么都不做（零开销路径）；
    4. Observer 缺失某钩子或钩子不可调用 → 跳过（防御性，抽象类已保证齐全）；
    5. ``asyncio.CancelledError`` 属于 ``BaseException``，**不被吞掉**。

    Args:
        observers: 观察者列表（``None`` 视为空）。
        hook_name: 钩子名，取值见 ``HOOK_ON_*`` 常量。
        execution: 本次执行状态。
        tool_execution: 仅 ``on_tool_call`` 事件需要（该次 Tool 调用记录）。

    Returns:
        None: 本函数无返回值，也不代表任何执行结果。
    """
    if not observers:
        return

    for observer in observers:
        hook = getattr(observer, hook_name, None)
        if hook is None or not callable(hook):
            continue
        try:
            if tool_execution is None:
                await hook(execution)
            else:
                await hook(execution, tool_execution)
        except Exception as exc:  # noqa: BLE001 - Observer 失败不影响主流程
            _observer_logger.warning(
                "observer hook failed (ignored) | observer=%s hook=%s "
                "error=%s: %s",
                getattr(observer, "name", type(observer).__name__),
                hook_name,
                type(exc).__name__,
                exc,
            )


__all__ = [
    "HOOK_ON_FINISH",
    "HOOK_ON_START",
    "HOOK_ON_TOOL_CALL",
    "MASKED_VALUE",
    "MAX_VALUE_LENGTH",
    "AgentExecutionObserver",
    "LoggingExecutionObserver",
    "build_default_observers",
    "notify_observers",
    "sanitize_arguments",
]
