"""基于标准库 ``logging`` 的执行观察者（最小可用实现）。

记录内容（一次 Agent 执行的三类事件）::

    agent execution started  | max_iterations=3
    agent tool call          | name=calculator tool_call_id=call_1 success=true ...
    agent execution finished | iterations=1 tool_calls=1 ... duration_ms=12.345

安全边界（重要）
----------------
日志中**绝不**输出密钥类信息：

- 本模块不读取 ``settings`` / 环境变量，因此不会打印 LLM_API_KEY /
  SECRET_KEY / DATABASE_URL / JWT；
- Tool 参数只透传参数本身，并在写入日志前经过 :func:`sanitize_arguments`：

  - 键名命中敏感关键字（password / secret / token / api_key /
    authorization / credential）→ 值被替换为 ``***``；
  - 超长字符串被截断（避免日志膨胀）；
  - 不做递归脱敏、不做复杂脱敏框架（当前阶段明确不引入）。

日志级别
--------
- ``on_start`` / ``on_tool_call`` / ``on_finish`` → ``INFO``（事件摘要）；
- Tool 结果正文 → ``DEBUG``（逐字结果只在排查时开启）。

本模块只依赖标准库 ``logging`` 与 observer / execution 抽象，
不依赖 FastAPI / Database / RAG / OpenAI SDK。
"""

import logging

from app.agents.execution import AgentExecution, ToolExecution
from app.agents.observers.base import AgentExecutionObserver

#: 本模块 logger（调用方可按需设置级别 / 处理器；本模块不修改全局日志配置）
logger = logging.getLogger(__name__)

#: 命中即脱敏的键名关键字（大小写不敏感）
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
)

#: 脱敏后的占位符
MASKED_VALUE = "***"

#: 单个字符串值的最大记录长度（超出即截断）
MAX_VALUE_LENGTH = 200


def _is_sensitive_key(key: object) -> bool:
    """键名是否命中敏感关键字（最小实现，不做模糊匹配框架）。"""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _truncate(value: str) -> str:
    """超长字符串截断（保留长度信息，便于排查）。"""
    if len(value) <= MAX_VALUE_LENGTH:
        return value
    return f"{value[:MAX_VALUE_LENGTH]}...(truncated, total={len(value)})"


def sanitize_arguments(arguments: object) -> object:
    """把 Tool 参数转换为可安全写入日志的形式。

    规则（明确、最小）：

    - 非 ``dict`` 参数：原样返回（Tool 参数按契约应是 dict，
      非 dict 属于异常输入，由 Registry 校验处理，日志不猜测结构）；
    - ``dict`` 参数：键名命中 :data:`SENSITIVE_KEY_MARKERS` 的值替换为
      :data:`MASKED_VALUE`；字符串值按 :data:`MAX_VALUE_LENGTH` 截断；
      其它类型（int / list / dict 等）原样保留以便调试。

    Args:
        arguments: Tool 参数（来自 ``ToolExecution.arguments``）。

    Returns:
        object: 可安全记录的对象（dict 或原值）。
    """
    if not isinstance(arguments, dict):
        return arguments
    return {
        key: (
            MASKED_VALUE
            if _is_sensitive_key(key)
            else (_truncate(value) if isinstance(value, str) else value)
        )
        for key, value in arguments.items()
    }


class LoggingExecutionObserver(AgentExecutionObserver):
    """把执行生命周期写入标准库 logging 的观察者。

    使用方式::

        runtime = AgentRuntime(provider, tool_registry, observers=[
            LoggingExecutionObserver(),
        ])

    Args:
        target_logger: 可选的自定义 logger（默认使用本模块 logger）。
            主要用于测试注入 / 应用自定义 handler。
    """

    #: 观察者名称
    name = "logging"

    def __init__(self, target_logger: logging.Logger | None = None) -> None:
        self._logger = target_logger or logger

    async def on_start(self, execution: AgentExecution) -> None:
        """记录执行开始（含 max_iterations）。"""
        self._logger.info(
            "agent execution started | max_iterations=%s messages=%s",
            execution.max_iterations,
            len(execution.messages),
        )

    async def on_tool_call(
        self,
        execution: AgentExecution,
        tool_execution: ToolExecution,
    ) -> None:
        """记录一次 Tool 调用结果（名称 / ID / 参数 / 是否成功）。"""
        self._logger.info(
            "agent tool call | name=%s tool_call_id=%s success=%s "
            "failed=%s iterations=%s arguments=%s",
            tool_execution.name,
            tool_execution.tool_call_id,
            not tool_execution.failed,
            tool_execution.failed,
            execution.iterations,
            sanitize_arguments(tool_execution.arguments),
        )
        self._logger.debug(
            "agent tool result | name=%s tool_call_id=%s result=%s",
            tool_execution.name,
            tool_execution.tool_call_id,
            _truncate(tool_execution.result),
        )

    async def on_finish(self, execution: AgentExecution) -> None:
        """记录执行结束（轮数 / Tool 统计 / 结束原因 / 耗时）。"""
        self._logger.info(
            "agent execution finished | iterations=%s tool_calls=%s "
            "failed_tool_calls=%s used_tools=%s stop_reason=%s "
            "reached_iteration_limit=%s duration_ms=%s",
            execution.iterations,
            len(execution.tool_calls),
            execution.failed_tool_calls,
            execution.used_tools,
            execution.stop_reason,
            execution.reached_iteration_limit,
            execution.duration_ms,
        )


__all__ = [
    "MASKED_VALUE",
    "MAX_VALUE_LENGTH",
    "SENSITIVE_KEY_MARKERS",
    "LoggingExecutionObserver",
    "logger",
    "sanitize_arguments",
]
