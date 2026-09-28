"""Conversation API Router。

薄 HTTP 层：认证 → DI → Service，不做任何业务/数据库操作。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.dependencies import get_agent_service, get_current_user
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreate,
    ConversationResponse,
    ConversationUpdate,
    PaginatedConversationsResponse,
)
from app.schemas.message import MessageResponse
from app.services.agent import AgentService

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
)


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建会话",
)
async def create_conversation(
    data: ConversationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> ConversationResponse:
    try:
        conversation = await service.create_conversation(current_user, data.title)
        return ConversationResponse.model_validate(conversation)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get(
    "",
    response_model=PaginatedConversationsResponse,
    summary="分页获取我的会话列表",
)
async def list_conversations(
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(
        default=20, ge=1, le=100, description="每页数量（最大 100）",
    ),
) -> PaginatedConversationsResponse:
    items, total = await service.list_conversations(
        current_user, page=page, page_size=page_size
    )
    return PaginatedConversationsResponse(
        items=[ConversationResponse.model_validate(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="获取会话详情",
)
async def get_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> ConversationResponse:
    try:
        conversation = await service.get_conversation(current_user, conversation_id)
        return ConversationResponse.model_validate(conversation)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageResponse],
    summary="获取会话历史消息",
    description=(
        "返回指定会话的全部消息（user / assistant），按时间正序排列。\n\n"
        "**只读接口**：消息由 ``POST /agent/chat`` 产生，不提供外部写入入口。\n"
        "**权限**：只能读取自己的会话（不存在 404 / 属于他人 403）。"
    ),
)
async def list_conversation_messages(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[MessageResponse]:
    """获取会话历史消息。

    前端在「切换会话 / 刷新页面」时调用本接口恢复聊天记录。
    """
    try:
        messages = await service.list_messages(current_user, conversation_id)
        return [MessageResponse.model_validate(m) for m in messages]
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.patch(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="重命名会话",
)
async def update_conversation(
    conversation_id: int,
    data: ConversationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> ConversationResponse:
    try:
        conversation = await service.update_conversation_title(
            current_user, conversation_id, data.title
        )
        return ConversationResponse.model_validate(conversation)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话",
)
async def delete_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> None:
    try:
        await service.delete_conversation(current_user, conversation_id)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
