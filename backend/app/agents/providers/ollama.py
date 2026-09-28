"""Ollama LLM Provider（OpenAI Compatible API）。

Ollama 本地服务默认地址 ``http://localhost:11434``，
其 OpenAI Compatible 端点为 ``/v1/chat/completions``。

- 不依赖 FastAPI / Depends / Database。
- 使用标准库 ``urllib`` 发起 HTTP 请求，不引入额外依赖。
- 未启动 Ollama 或模型未运行时，调用会抛出 ``URLError`` / ``HTTPError``，
  由上层自行处理（本阶段不做真实 API 测试）。
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

# Ollama 本地默认 OpenAI Compatible 端点
OLLAMA_BASE_URL = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    """Ollama 本地模型 Provider。

    通过 ``settings.LLM_MODEL``（默认 qwen）选择本地模型。
    可通过 ``base_url`` / ``model`` 参数覆盖默认配置。
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.base_url = (base_url or OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        self.temperature = (
            temperature if temperature is not None else settings.LLM_TEMPERATURE
        )

    def _build_payload(self, messages: list[dict]) -> dict:
        """构造 /v1/chat/completions 请求体（不包含 tools）。"""
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }

    def _post_chat_completions(self, payload: dict) -> dict:
        """向 Ollama ``/v1/chat/completions`` 发起请求并返回解析后的 JSON。

        统一处理 HTTP / 网络层异常，供普通对话与 Tool Calling 复用。
        """
        url = f"{self.base_url}/v1/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTP 层错误：保留上游状态码与响应体（如模型未拉取）
            body = exc.read().decode("utf-8", errors="replace")
            raise LLMProviderError(
                f"Ollama API 调用失败 (HTTP {exc.code}): {body}"
            ) from exc
        except urllib.error.URLError as exc:
            # 网络层错误：Ollama 服务未启动 / 地址不可达
            raise LLMProviderError(
                f"无法连接 Ollama 服务 ({self.base_url}): {exc.reason}。"
                "请确认本地 Ollama 已启动。"
            ) from exc

    async def generate(self, messages: list[dict]) -> str:
        """调用 Ollama OpenAI Compatible API 生成回答。"""

        def _sync_call() -> str:
            data = self._post_chat_completions(self._build_payload(messages))
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMProviderError(
                    f"Unexpected Ollama API response: {data}"
                )

        return await asyncio.to_thread(_sync_call)

    async def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """携带 ``tools`` 调用 Ollama（原生 Tool Calling）。

        Ollama 的 OpenAI Compatible 端点（``/v1/chat/completions``）支持标准
        ``tools`` 参数（需模型本身支持 function calling）。
        这里直接透传工具 Schema，并解析响应中的 ``tool_calls``。
        """

        def _sync_call() -> LLMResponse:
            payload = self._build_payload(messages)
            payload["tools"] = tools
            data = self._post_chat_completions(payload)
            return parse_chat_completion(data, provider_label="Ollama")

        return await asyncio.to_thread(_sync_call)
