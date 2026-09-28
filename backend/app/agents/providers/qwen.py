"""Qwen LLM Provider（OpenAI Compatible API）。

- 当 ``settings.LLM_API_KEY`` 为空时：不发起网络请求，返回本地模拟回答，
  用于无外部依赖的教学/演示环境打通完整链路。
- 当配置了 ``LLM_API_KEY`` 时：调用 Qwen 的 OpenAI Compatible 接口。

使用标准库 ``urllib`` 发起 HTTP 请求，不引入额外依赖。
"""

import asyncio
import json

import urllib.error
import urllib.request

from app.agents.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    parse_chat_completion,
)
from app.core.config import settings

# Qwen（DashScope）OpenAI Compatible 端点
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenProvider(LLMProvider):
    """Qwen 对话模型 Provider。

    通过 ``settings.LLM_MODEL``（默认 qwen）选择模型。
    可通过 ``base_url`` 覆盖默认端点（优先使用 ``settings.LLM_BASE_URL``）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.base_url = (base_url or settings.LLM_BASE_URL or QWEN_BASE_URL).rstrip(
            "/"
        )
        self.temperature = (
            temperature if temperature is not None else settings.LLM_TEMPERATURE
        )

    async def generate(self, messages: list[dict]) -> str:
        # 未配置 API Key：教学模式下返回模拟回答，保持完整链路可运行
        if not self.api_key:
            return self._fallback_reply(messages)

        return await self._call_qwen(messages)

    def _fallback_reply(self, messages: list[dict]) -> str:
        """本地模拟回复（不触发网络请求）。

        仅用于未配置 ``LLM_API_KEY`` 时演示 HTTP -> Agent -> LLM 链路。
        配置真实 Key 后不会走此分支。
        """
        user_msg = next(
            (m.get("content", "")
             for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return f"你好！我已收到你的消息：{user_msg}。（当前未配置 LLM_API_KEY，这是本地模拟回复。）"

    def _build_payload(self, messages: list[dict]) -> dict:
        """构造 /chat/completions 请求体（不包含 tools）。

        本项目只使用**非流式** Chat Completions（不实现 Streaming / SSE），
        因此这里固定声明 ``stream=False``。

        Qwen3 系列模型默认开启思考模式，而**非流式**调用要求显式关闭：
        缺少 ``enable_thinking=False`` 时上游返回 HTTP 400
        ``parameter.enable_thinking must be set to false for non-streaming calls``。
        （关闭思考模式不影响回答内容与既有调用链，仅决定是否返回思考过程。）
        """
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
            "enable_thinking": False,
        }

    def _post_chat_completions(self, payload: dict) -> dict:
        """向 Qwen ``/chat/completions`` 发起请求并返回解析后的 JSON。

        统一处理 HTTP / 网络层异常，供普通对话与 Tool Calling 复用。
        """
        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTP 层错误：保留上游状态码与响应体，便于定位问题
            body = exc.read().decode("utf-8", errors="replace")
            raise LLMProviderError(
                f"Qwen API 调用失败 (HTTP {exc.code}): {body}"
            ) from exc
        except urllib.error.URLError as exc:
            # 网络层错误：Base URL 无效 / 无法连接
            raise LLMProviderError(
                f"无法连接 Qwen API ({self.base_url}): {exc.reason}"
            ) from exc

    async def _call_qwen(self, messages: list[dict]) -> str:
        """调用 Qwen OpenAI Compatible API。"""

        def _sync_call() -> str:
            data = self._post_chat_completions(self._build_payload(messages))
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMProviderError(
                    f"Unexpected Qwen API response: {data}"
                )

        return await asyncio.to_thread(_sync_call)

    async def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """携带 ``tools`` 调用 Qwen（原生 Tool Calling）。

        Qwen 的 OpenAI Compatible 端点支持标准 ``tools`` 参数，
        因此这里直接透传工具 Schema，并解析响应中的 ``tool_calls``。

        - 未配置 ``LLM_API_KEY`` 时与 :meth:`generate` 一致，
          返回本地模拟回复（不发起网络请求、不伪造工具调用）。
        """
        # 未配置 API Key：保持与普通对话相同的本地模拟行为
        if not self.api_key:
            return LLMResponse(content=self._fallback_reply(messages))

        def _sync_call() -> LLMResponse:
            payload = self._build_payload(messages)
            payload["tools"] = tools
            data = self._post_chat_completions(payload)
            return parse_chat_completion(data, provider_label="Qwen")

        return await asyncio.to_thread(_sync_call)
