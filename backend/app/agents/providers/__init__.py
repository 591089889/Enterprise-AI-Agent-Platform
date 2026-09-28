"""LLM Provider 包。

提供统一抽象（:class:`LLMProvider`）与具体实现
（:class:`QwenProvider`、:class:`OllamaProvider`）。

通过简单工厂 :func:`get_llm_provider` 按 ``settings.LLM_PROVIDER``
（qwen / ollama）返回对应 Provider 实例。
"""

from app.agents.providers.base import LLMProvider, LLMProviderError
from app.agents.providers.ollama import OllamaProvider
from app.agents.providers.qwen import QwenProvider
from app.core.config import settings


#: 当前支持的 Provider 名称
SUPPORTED_PROVIDERS: tuple[str, ...] = ("qwen", "ollama")


def get_llm_provider(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> LLMProvider:
    """返回 LLM Provider 实例。

    参数（Stage 17 新增，均为可选）
    ------------------------------
    - ``provider``：Provider 名称。为 ``None`` 时使用 ``settings.LLM_PROVIDER``
      （保持既有行为不变）；
    - ``model``：模型名。为 ``None`` 时由各 Provider 使用 ``settings.LLM_MODEL``；
    - ``temperature``：生成温度。为 ``None`` 时使用 ``settings.LLM_TEMPERATURE``。

    用途：``AgentService`` 读取 Admin 配置的 Agent（provider / model /
    temperature）后，通过本工厂构建该次对话的 Provider —— 复用同一套
    Provider 抽象与实现，**不新增任何 Provider 架构**。

    支持:
        - ``qwen``   → :class:`QwenProvider`
        - ``ollama`` → :class:`OllamaProvider`

    职责边界
    --------
    本工厂**只负责实例选择**：
    - 不做业务逻辑；
    - 不发 HTTP 请求；
    - 不访问数据库 / RAG。

    保持简单：仅 if/elif 映射，不引入 Provider Registry / Plugin 机制。

    Raises:
        ValueError: 当 Provider 名称不是受支持的名称时抛出。
            明确报错而不是静默回退到默认 Provider，避免配置错误被掩盖。
    """
    provider_name = (provider or settings.LLM_PROVIDER or "qwen").strip().lower()

    if provider_name == "qwen":
        return QwenProvider(model=model, temperature=temperature)
    if provider_name == "ollama":
        return OllamaProvider(model=model, temperature=temperature)

    supported = ", ".join(SUPPORTED_PROVIDERS)
    raise ValueError(
        f"不支持的 LLM_PROVIDER='{provider or settings.LLM_PROVIDER}'。"
        f"当前支持的 Provider: {supported}。"
        "请检查 backend/.env 中的 LLM_PROVIDER 配置或 Agent 配置中的 provider。"
    )


__all__ = [
    "LLMProvider",
    "LLMProviderError",
    "OllamaProvider",
    "QwenProvider",
    "get_llm_provider",
]
