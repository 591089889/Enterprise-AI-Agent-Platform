"""Agent API Router。

负责将 HTTP 请求映射到 AgentService 的 chat 方法。

完整调用链::

    POST /agent/chat
      ↓
    Router（本模块）
      ↓  Depends(get_current_user)   # Authentication
      ↓  Depends(get_agent_service)  # DI
      ↓
    AgentService.chat()
      ↓
    AgentRuntime.run()
      ↓
    LLMProvider.generate()
      ↓
    LLM API / 本地模拟
      ↓
    HTTP 200 + AgentChatResponse

错误边界（Stage 10）
-------------------

本 Router 是 **API 边界**，只做两件事：

1. ``AppException``（会话不存在 404 / 无权限 403 等）→ HTTPException，
   由全局处理器输出统一结构；
2. ``LLMProviderError``（上游 LLM 服务网络 / 协议故障）→
   ``UpstreamServiceException``（HTTP 502）。

以下错误**不在这里处理**，行为与 Stage 8 / 9 完全一致：

- **Tool Error**（``UnknownToolError`` / ``ToolParameterError`` /
  ``ToolExecutionError``）：由 ``AgentRuntime`` 捕获并作为
  ``Tool error: ...`` 回传给 LLM，正常返回 200，**不会**变成 500；
- **RAG 检索失败**：由 ``AgentService`` 静默降级，不影响对话。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.agents.providers import LLMProviderError
from app.core.dependencies import get_agent_service, get_current_user
from app.core.exceptions import AppException, UpstreamServiceException
from app.models.user import User
from app.schemas.agent import AgentChatRequest, AgentChatResponse
from app.services.agent import AgentService

#: 上游 LLM 服务不可用时的固定文案。
#: 不透传 Provider 原始错误（可能包含 URL / 上游响应体 / Key 等内部信息）。
UPSTREAM_UNAVAILABLE_MESSAGE = "AI 模型服务暂时不可用，请稍后重试"

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
)


@router.post(
    "/chat",
    response_model=AgentChatResponse,
    summary="Agent 对话",
    description="发送消息给 AI Agent，返回回答。需要登录并指定会话。",
)
async def chat(
    data: AgentChatRequest,
    # 已认证用户（401 由 get_current_user 处理）
    current_user: Annotated[User, Depends(get_current_user)],
    # AgentService（DI 构建 Runtime + Provider 链路）
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentChatResponse:
    """Agent 对话接口。

    流程：
    1. 认证当前用户（Depends(get_current_user)）。
    2. 注入 AgentService（Depends(get_agent_service)）。
    3. 调用 service.chat(current_user, conversation_id, message)。
    4. 返回 AgentChatResponse（含 conversation_id + reply）。

    错误映射（只发生在 API 边界）：
    - ``AppException``（404 / 403 等）→ 对应 HTTP 状态码 + 统一错误结构；
    - ``LLMProviderError``（上游 LLM 故障）→ HTTP 502 + 固定文案；
    - Tool 失败**不在此处**：Runtime 已把它转成 ``Tool error: ...``
      回传给 LLM，接口仍然返回 200。
    """
    try:
        reply = await service.chat(
            current_user,
            data.conversation_id,
            data.message,
            data.agent_id,
        )
    except LLMProviderError as exc:
        # Provider 层错误（网络 / HTTP / 协议）：只记录原因，不透传给客户端
        raise UpstreamServiceException(UPSTREAM_UNAVAILABLE_MESSAGE) from exc
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return AgentChatResponse(
        conversation_id=data.conversation_id,
        reply=reply,
    )
