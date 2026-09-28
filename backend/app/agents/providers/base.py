"""LLM Provider 抽象接口。

所有 LLM Provider（OpenAI / Qwen / DeepSeek / SiliconFlow 等）
实现统一接口，使 AgentRuntime 不绑定任何具体厂商 SDK。

当前 Agent Core 第一版只定义一个方法 ``generate``。

错误约定
--------
Provider 的网络 / HTTP / 协议错误统一抛出 :class:`LLMProviderError`
（``RuntimeError`` 子类，历史契约不变），由上层决定如何呈现：
AgentRuntime 原样向上传播，API 边界将其转换为统一的 HTTP 错误响应。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """LLM 发起的一次工具调用请求。

    Attributes:
        id: 调用唯一 ID（回传 tool_result 时必须带同一个 id）。
        name: 要调用的工具名（对应 Tool.name）。
        arguments: LLM 解析出的参数（已从 JSON 字符串解析为 dict）。
    """

    id: str
    name: str
    arguments: dict = field(default_factory=dict)

    def to_openai_dict(self) -> dict:
        """转换为 OpenAI ``assistant.tool_calls`` 元素格式。

        回传历史时需要把 assistant 的 tool_calls 以该格式放回 messages。
        """
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class LLMResponse:
    """LLM 的一次返回结果。

    LLM 的返回有两种可能：

    1. 直接给出最终回答（``content`` 有值，``tool_calls`` 为空）；
    2. 要求调用工具（``tool_calls`` 非空，``content`` 可能为空）。

    Attributes:
        content: 文本内容（最终回答或工具调用前的说明）。
        tool_calls: LLM 要求执行的工具调用列表。
    """

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        """是否包含工具调用请求。"""
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """LLM 统一抽象接口。

    所有具体 Provider 都应继承本类并实现 ``generate``。

    设计说明
    --------
    - ``generate`` 接收 ``list[dict]`` 形式的消息序列（OpenAI 风格），
      返回纯文本回答。
    - 具体实现内部负责 API Key、模型名、HTTP 调用、错误处理。
    - AgentRuntime 只依赖本接口，不感知具体厂商。
    """

    @abstractmethod
    async def generate(self, messages: list[dict]) -> str:
        """调用 LLM 生成回答。

        Args:
            messages: OpenAI 风格消息序列，例如::

                [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "你好"},
                ]

        Returns:
            模型生成的回答文本。
        """

    async def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """携带工具定义调用 LLM（默认实现，兼容不支持原生 Tool Calling 的厂商）。

        默认行为：**忽略 ``tools``**，退化为一次普通 :meth:`generate` 调用，
        返回不含 ``tool_calls`` 的 :class:`LLMResponse`。

        设计意图
        --------
        - 这是「可选能力」而非「必须实现」：已有的 Qwen / Ollama Provider
          即使不覆写本方法也不会报错，行为与加 Tool 之前完全一致（零回归）。
        - 支持 OpenAI Compatible ``tools`` 参数的 Provider 可以覆写本方法，
          返回真正带有 ``tool_calls`` 的 :class:`LLMResponse`。

        Args:
            messages: OpenAI 风格消息序列。
            tools: 工具 Schema 列表（OpenAI function calling 格式）。

        Returns:
            LLMResponse: 模型返回结果（默认实现只会包含文本）。
        """
        text = await self.generate(messages)
        return LLMResponse(content=text)


class LLMProviderError(RuntimeError):
    """LLM Provider 调用失败（网络 / HTTP / 协议错误）。

    为什么需要它
    ------------
    之前 Provider 抛裸 ``RuntimeError``，上层无法区分
    「上游 Provider 故障」与「本服务自身的代码缺陷」，
    因此无法在 API 边界给出正确的 HTTP 语义（502 vs 500）。

    为什么继承 ``RuntimeError``
    ---------------------------
    - **契约不变**：Provider 一直以 ``RuntimeError`` 报错，AgentRuntime
      的行为也依赖「Provider 异常原样向上传播、不被 Tool 错误边界吞掉」；
      继承 ``RuntimeError`` 保证既有代码与测试零回归。
    - **语义增强**：新增可精确识别的类型，仅供上层做错误分类。

    约定：Provider 实现必须抛出本异常（或子类），不要抛裸 ``RuntimeError``。
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def parse_chat_completion(
    data: dict,
    provider_label: str = "LLM",
) -> LLMResponse:
    """把 OpenAI Compatible ``/chat/completions`` 响应解析为 :class:`LLMResponse`。

    Qwen 与 Ollama 都使用 OpenAI 兼容协议，因此响应解析逻辑共用本函数，
    避免在两个 Provider 中重复实现。

    支持两种返回：
    - 只有 ``content``（最终回答）；
    - 带 ``message.tool_calls``（要求调用工具）。

    Args:
        data: 已解析的响应 JSON。
        provider_label: 出错时用于标识来源（如 "Qwen"）。

    Returns:
        LLMResponse: 统一的返回结构。

    Raises:
        LLMProviderError: 响应结构异常 / tool_calls 非法时抛出
            （明确报错，不吞掉；``LLMProviderError`` 是 ``RuntimeError``
            子类，历史契约保持不变）。
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMProviderError(
            f"Unexpected {provider_label} API response: {data}"
        ) from exc

    tool_calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        try:
            function = raw["function"]
            name = function["name"]
        except (KeyError, TypeError) as exc:
            raise LLMProviderError(
                f"{provider_label} 返回了非法的 tool_calls 元素: {raw}"
            ) from exc

        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LLMProviderError(
                    f"{provider_label} 返回的 tool_call arguments 不是合法 JSON: "
                    f"{raw_arguments!r}"
                ) from exc

        tool_calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{index}",
                name=name,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )

    return LLMResponse(content=message.get("content"), tool_calls=tool_calls)
