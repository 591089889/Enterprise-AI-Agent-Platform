"""Agent 运行时（框架无关）。

第一版职责：接收用户消息，组装 prompt，调用 LLM Provider，返回回答。

设计说明
--------
- 不依赖 FastAPI / Database / Repository。
- 不感知 RAG / VectorStore / Embedding / ChromaDB。
- 通过 ``LLMProvider`` 抽象与具体 LLM 厂商解耦。
- ``system_prompt`` 由上层（AgentService）按需传入，
  Runtime 只负责把它作为首条 system 消息传给 Provider。
- Tool Calling：通过可选的 ``ToolRegistry`` 把工具 Schema 交给 LLM，
  并在 LLM 要求调用工具时执行 Tool、把结果回传给 LLM，直到得到最终回答。
  Runtime 只认识 ``ToolRegistry`` / ``Tool`` 抽象，**不依赖任何具体 Tool**。
- 执行过程由 :class:`~app.agents.execution.AgentExecution` 显式表达
  （messages / iterations / tool_calls / tool_results / final_response /
  stop_reason），使「一次 Agent 执行」可观察、可测试；不引入状态机框架。
- 循环有明确上界（``max_iterations``，默认 :data:`MAX_TOOL_ITERATIONS`），
  达到上界后一定会返回最终回答，不存在无法结束的路径。
- **执行可观测性（Stage 9）**：Runtime 在生命周期节点（开始 / 每次 Tool 调用
  结束 / 正常结束）通过 :func:`~app.agents.observers.notify_observers` 通知
  ``AgentExecutionObserver``。Observer 只观察不控制，其异常被兜住，不影响
  Agent 主流程；Runtime 只依赖 Observer 抽象，不依赖任何具体 Observer 实现。
"""

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
    AgentExecutionObserver,
    notify_observers,
)
from app.agents.providers import LLMProvider
from app.agents.providers.base import ToolCall
from app.agents.tools.base import ToolError
from app.agents.tools.registry import ToolRegistry

# 系统提示词：第一版使用固定提示词，未来可改为持久化的 Agent 配置
DEFAULT_SYSTEM_PROMPT = (
    "你是一个企业级 AI Agent 助手。请用简洁、专业、中文回答用户问题。"
)

#: 单次对话中「LLM → Tool → LLM」的最大轮数（防止工具调用无限循环）。
#: 达到该轮数后 Runtime 不再携带 tools，只索取一次最终回答。
MAX_TOOL_ITERATIONS = 3

#: 模型返回空响应（``None`` / 空字符串 / 纯空白）时的最终回答兜底文案。
#: 目的：把「空响应」变成明确、可预期的结果，避免上层保存空消息。
EMPTY_RESPONSE_FALLBACK = "（模型未返回内容，请重试或换一种问法）"


class AgentRuntime:
    """Agent 执行引擎。

    Args:
        provider: 实现了 :class:`LLMProvider` 接口的 LLM 适配器。
        tool_registry: 可选的 Tool 注册表。为 ``None`` 时不启用 Tool Calling，
            行为与未引入 Tool 之前完全一致。
        max_iterations: 可选的 Tool Calling 轮数上限，默认
            :data:`MAX_TOOL_ITERATIONS`。主要用于测试严格控制循环边界；
            生产路径使用默认值。
        observers: 可选的执行观察者列表（:class:`AgentExecutionObserver`）。
            为 ``None`` / 空列表时不产生任何观察开销。由 DI 注入，
            Runtime **不创建也不硬编码**任何具体 Observer。

    Raises:
        ValueError: ``max_iterations`` 小于 1 时抛出（明确报错，
            不静默改成默认值，避免「以为限了 0 轮」的配置错误）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
        observers: list[AgentExecutionObserver] | None = None,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(
                f"max_iterations 必须 >= 1，收到: {max_iterations}"
            )
        self.provider = provider
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        # 复制为新列表：调用方后续修改自己的列表不会影响本 Runtime，
        # 也避免多个 Runtime 共享同一可变序列。
        self.observers: list[AgentExecutionObserver] = list(observers or [])

    async def run(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
    ) -> str:
        """执行一次 Agent 对话，返回最终回答文本（对外契约保持不变）。

        流程::

            无可用 Tool  → Provider.generate()                 （普通对话）
            有可用 Tool  → Provider.generate_with_tools()
                             ├─ 无 tool_calls → 返回最终回答
                             └─ 有 tool_calls → ToolRegistry.execute()
                                                 → 结果回传 LLM → 再问一次

        本方法只是 :meth:`execute` 的薄封装（返回 ``final_response``）。
        需要观察执行过程（iterations / tool_calls / tool_results /
        stop_reason）时请直接使用 :meth:`execute`。

        Args:
            messages: OpenAI 风格消息序列（含历史 user/assistant）。
            system_prompt: 可选的 system 提示词。为 ``None`` 时使用
                :data:`DEFAULT_SYSTEM_PROMPT`。上层可通过该参数注入
                RAG context 等额外参考信息，而 Runtime 本身不感知
                这些信息的来源。

        Returns:
            str: LLM 生成的最终回答文本（一定非空，空响应会退化为
            :data:`EMPTY_RESPONSE_FALLBACK`）。
        """
        execution = await self.execute(messages, system_prompt=system_prompt)
        return execution.final_response

    async def execute(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
    ) -> AgentExecution:
        """执行一次 Agent 对话，返回完整执行状态 :class:`AgentExecution`。

        状态字段含义见 :class:`~app.agents.execution.AgentExecution`。

        执行边界
        --------
        - **不污染调用方消息**：``messages`` 被复制进新的列表，Runtime 只往
          副本里追加 assistant / tool 消息。
        - **一定结束**：最多执行 ``self.max_iterations`` 轮 Tool Calling；
          达到上限后不再携带 tools，只索取一次最终回答。
        - **错误分层**：Tool 层错误（:class:`ToolError` 及 Tool 抛出的其他
          异常）在此被转换为 ``Tool error: ...`` 结果回传给 LLM；
          Provider 异常（网络 / 协议错误）**不在此吞掉**，原样向上传播，
          由上层决定如何呈现。
        - **可观测性（Stage 9）**：通知顺序为
          ``on_start`` → （``on_tool_call``）* → ``on_finish``。
          ``on_start`` 在调用 Provider 之前触发；``on_finish`` 只在**正常
          结束**（``stop_reason`` 已设置）后触发，Provider 异常导致的中断
          不会触发 ``on_finish``（不伪造正常结束）。Observer 的异常不会
          影响本次执行的返回值。

        Args:
            messages: OpenAI 风格消息序列（含历史 user/assistant）。
            system_prompt: 可选的 system 提示词（``None`` 时用默认提示词）。

        Returns:
            AgentExecution: 本次执行的完整状态。
        """
        execution = AgentExecution(
            messages=self._build_messages(messages, system_prompt),
            max_iterations=self.max_iterations,
        )

        # 1) 执行开始：记录起始时间并通知观察者（在任何 LLM 调用之前）
        execution.mark_started()
        await notify_observers(self.observers, HOOK_ON_START, execution)

        # 2) 执行主体（普通对话 / Tool Calling 循环）
        tools = self._available_tool_schemas()
        if not tools:
            # 没有可用工具：走普通文本生成（与引入 Tool 之前一致）
            execution.final_response = self._normalize_answer(
                await self.provider.generate(execution.messages)
            )
            execution.stop_reason = STOP_REASON_FINAL_ANSWER
        else:
            await self._run_tool_loop(execution, tools)

        # 3) 执行正常结束：记录结束时间与耗时，并通知观察者
        execution.mark_finished()
        await notify_observers(self.observers, HOOK_ON_FINISH, execution)
        return execution

    @staticmethod
    def _build_messages(
        messages: list[dict],
        system_prompt: str | None,
    ) -> list[dict]:
        """组装完整消息序列（system + 调用方历史）。

        返回**新列表**：后续 Tool Calling 追加的 assistant / tool 消息只影响
        该副本，调用方传入的 ``messages``（原始 conversation history）不被修改。

        Args:
            messages: OpenAI 风格消息序列（含历史 user/assistant）。
            system_prompt: 可选的 system 提示词，``None`` 时使用默认提示词。

        Returns:
            list[dict]: 含 system 首条消息的完整消息序列（新列表）。
        """
        return [
            {
                "role": "system",
                "content": system_prompt or DEFAULT_SYSTEM_PROMPT,
            },
            *messages,
        ]

    # ------------------------------------------------------------------
    # Tool Calling 循环
    # ------------------------------------------------------------------
    def _available_tool_schemas(self) -> list[dict]:
        """获取当前可用工具的 Schema 列表（供 LLM 判断可调用哪些工具）。"""
        if self.tool_registry is None:
            return []
        return self.tool_registry.get_schemas()

    async def _run_tool_loop(
        self,
        execution: AgentExecution,
        tools: list[dict],
    ) -> None:
        """执行「LLM → Tool → LLM」循环，并把过程写入 ``execution``（原地更新）。

        循环规则（Stage 8 明确定义）::

            每一轮：generate_with_tools(execution.messages, tools)
                ├─ 无 tool_calls → 该轮文本即最终回答
                │                  stop_reason = final_answer，循环结束
                └─ 有 tool_calls → iterations += 1
                                   追加 assistant.tool_calls 消息
                                   逐个执行 Tool，结果作为 tool 消息追加
                                   → 进入下一轮

            达到 max_iterations 仍然要求调用工具
                → 不再携带 tools 调用一次 generate()（避免继续要求调用工具）
                → stop_reason = max_iterations，保证一定有最终回答返回

        说明：``iterations`` 只统计「确实要求调用工具」的轮数，因此
        ``iterations <= max_iterations`` 恒成立，不存在无限循环路径。

        Args:
            execution: 本次执行的状态（原地更新 messages / iterations /
                tool_calls / tool_results / final_response / stop_reason）。
            tools: 工具 Schema 列表。
        """
        while execution.iterations < execution.max_iterations:
            response = await self.provider.generate_with_tools(
                execution.messages, tools
            )

            # LLM 直接给出最终回答 → 结束
            if not response.has_tool_calls:
                execution.final_response = self._normalize_answer(
                    response.content
                )
                execution.stop_reason = STOP_REASON_FINAL_ANSWER
                return

            execution.iterations += 1

            # 1) 记录 assistant 的 tool_calls（保持会话完整）
            execution.messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        call.to_openai_dict() for call in response.tool_calls
                    ],
                }
            )

            # 2) 执行工具，并把 tool_result 回传给 LLM
            #    （每个 tool_call 恰好执行一次，且恰好回传一条 tool 消息）
            for call in response.tool_calls:
                execution.tool_calls.append(call)
                tool_result = await self._execute_tool(call)
                execution.tool_results.append(tool_result)
                execution.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result.result,
                    }
                )
                # 3) 通知观察者（成功与失败都通知；Observer 异常被兜住）
                await notify_observers(
                    self.observers,
                    HOOK_ON_TOOL_CALL,
                    execution,
                    tool_result,
                )

        # 达到轮数上限：再要一次最终回答（不再携带 tools，避免继续调用）
        execution.final_response = self._normalize_answer(
            await self.provider.generate(execution.messages)
        )
        execution.stop_reason = STOP_REASON_MAX_ITERATIONS

    async def _execute_tool(self, call: ToolCall) -> ToolExecution:
        """执行单个 Tool 调用，返回执行记录（Tool 失败绝不中断 Agent 循环）。

        错误边界（Runtime 只做「兜底转换」，不做任何 Tool 业务判断）

        1. :class:`ToolError` 子类（Unknown / Parameter / Execution）
           → 以 ``Tool error: <message>`` 作为 tool_result 回传给 LLM，
             使模型有机会纠正参数或向用户说明，而不是静默吞掉异常。
        2. 非预期异常（Tool 实现自身的 bug，如 ``ValueError`` / ``TypeError``）
           → 同样转换为 ``Tool error: ...`` 文本。原因：OpenAI 协议要求
             「每个 tool_call_id 必须有且只有一条 tool 消息」，
             缺失会导致下一轮请求被上游拒绝；同时保证一次异常不会
             让整个 Agent 执行失败。
        3. Tool 返回非字符串（``None`` / ``int`` / ``dict`` 等）
           → 归一化为字符串（``None`` 视为未返回结果，按错误回传），
             避免把非法类型塞进 messages。
        4. 任务取消（``asyncio.CancelledError`` 属于 ``BaseException``）
           → 不会被上面的 ``except Exception`` 捕获，取消行为不受影响。

        Args:
            call: LLM 发起的一次工具调用。

        Returns:
            ToolExecution: 含 tool_call_id / 工具名 / **原始参数** / 结果文本。
        """
        # 记录 LLM 给出的原始参数（不修改、不补默认值）
        arguments = call.arguments if isinstance(call.arguments, dict) else {}

        if self.tool_registry is None:  # pragma: no cover - 防御性分支
            return ToolExecution(
                tool_call_id=call.id,
                name=call.name,
                arguments=arguments,
                result=f"{TOOL_ERROR_PREFIX} 当前未启用任何工具",
            )

        try:
            raw_result = await self.tool_registry.execute(call.name, arguments)
        except ToolError as exc:
            return ToolExecution(
                tool_call_id=call.id,
                name=call.name,
                arguments=arguments,
                result=f"{TOOL_ERROR_PREFIX} {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001 - 见上方错误边界第 2 点
            return ToolExecution(
                tool_call_id=call.id,
                name=call.name,
                arguments=arguments,
                result=(
                    f"{TOOL_ERROR_PREFIX} 工具 '{call.name}' 执行时发生"
                    f"未预期错误: {type(exc).__name__}: {exc}"
                ),
            )

        return ToolExecution(
            tool_call_id=call.id,
            name=call.name,
            arguments=arguments,
            result=self._normalize_tool_result(call.name, raw_result),
        )

    @staticmethod
    def _normalize_tool_result(name: str, result: object) -> str:
        """把 Tool 返回值归一化为字符串（``None`` 视为未返回结果，按错误回传）。

        Args:
            name: 工具名称（仅用于错误文案）。
            result: ``Tool.execute()`` 的返回值。

        Returns:
            str: 可直接作为 tool 消息 ``content`` 的文本。
        """
        if result is None:
            return f"{TOOL_ERROR_PREFIX} 工具 '{name}' 未返回结果"
        if isinstance(result, str):
            return result
        return str(result)

    @staticmethod
    def _normalize_answer(content: str | None) -> str:
        """最终回答归一化：空响应（``None`` / 空串 / 纯空白）→ 明确兜底文案。

        非空内容原样返回（不做 strip 等改写，避免影响模型输出）。

        Args:
            content: Provider 返回的文本（可能为 ``None``）。

        Returns:
            str: 非空的最终回答文本。
        """
        if content and content.strip():
            return content
        return EMPTY_RESPONSE_FALLBACK

