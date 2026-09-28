"""Agent 配置管理 API Router（``/agents``）。

与 ``/agent/chat`` 的分工
------------------------
- ``POST /agent/chat``：Agent 对话**执行**（Stage 3-9 已实现，本阶段不改动）；
- ``/agents``         ：Agent **配置管理**（Stage 15：CRUD）。

两者共存，互不影响：本模块只读写 ``agents`` 表，不触碰对话链路。

权限
----
Agent 管理属于企业管理功能：所有**管理**接口都要求管理员权限
（``Depends(require_admin)``）——未登录 401、普通用户 403。
前端隐藏菜单不能替代这里的后端校验。

唯一例外：``GET /agents/available``（Stage 17）面向**普通用户**，
只需登录即可访问，且只返回 ``id / name / description``
（Chat 页面的 Agent 选择器需要它；system_prompt 等管理配置不外泄）。

事务
----
Router 不控制事务：commit / rollback 由 ``get_db`` 统一负责
（``get_agent_service`` 内部依赖 ``DbSession``）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.dependencies import (
    get_agent_service,
    get_current_user,
    require_admin,
)
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.agent import (
    AgentAvailableResponse,
    AgentCreate,
    AgentResponse,
    AgentToolOption,
    AgentUpdate,
)
from app.services.agent import AgentService

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
)


@router.get(
    "",
    response_model=list[AgentResponse],
    summary="获取 Agent 配置列表",
    description="返回全部 Agent 配置。需要管理员权限。",
)
async def list_agents(
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[AgentResponse]:
    """获取 Agent 配置列表（按创建时间正序）。"""
    agents = await service.list_agents()
    return [AgentResponse.model_validate(agent) for agent in agents]


@router.get(
    "/available",
    response_model=list[AgentAvailableResponse],
    summary="获取可用 Agent 列表（Chat 页面）",
    description=(
        "返回可供对话使用的 Agent 列表。只需登录即可访问（Stage 17："
        "普通用户在 Chat 页面需要选择 Agent）。\\n\\n"
        "只返回 id / name / description，不暴露 system_prompt 等管理配置。"
    ),
)
async def list_available_agents(
    _current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[AgentAvailableResponse]:
    """获取可用 Agent 列表（供用户在 AI 工作台选择）。"""
    agents = await service.list_available_agents()
    return [AgentAvailableResponse.model_validate(agent) for agent in agents]


@router.get(
    "/tools",
    response_model=list[AgentToolOption],
    summary="获取可绑定的工具列表",
    description=(
        "返回后端已注册的内置工具（唯一来源：ToolRegistry）。需要管理员权限。\\n\\n"
        "Agent 的 ``tool_names`` 只能取本列表中的值；未注册的名称会被后端拒绝（400）。"
    ),
)
async def list_agent_tools(
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[AgentToolOption]:
    """获取可绑定给 Agent 的工具列表（管理页面选项来源）。"""
    tools = service.list_available_tools()
    return [AgentToolOption.model_validate(tool) for tool in tools]


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Agent 配置",
    description=(
        "创建 Agent 配置。需要管理员权限。\n\n"
        "只保存配置，不调用 LLM 验证 model（执行时才真正使用）。"
    ),
)
async def create_agent(
    data: AgentCreate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentResponse:
    """创建 Agent 配置。"""
    try:
        agent = await service.create_agent(data)
        return AgentResponse.model_validate(agent)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="获取 Agent 配置详情",
    description="按 ID 获取 Agent 配置。需要管理员权限。",
)
async def get_agent(
    agent_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentResponse:
    """获取指定 Agent 配置。"""
    try:
        agent = await service.get_agent(agent_id)
        return AgentResponse.model_validate(agent)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.patch(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="更新 Agent 配置",
    description=(
        "部分更新 Agent 配置（只更新传入的字段）。需要管理员权限。"
    ),
)
async def update_agent(
    agent_id: int,
    data: AgentUpdate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentResponse:
    """更新 Agent 配置。"""
    try:
        agent = await service.update_agent(agent_id, data)
        return AgentResponse.model_validate(agent)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除 Agent 配置",
    description="删除指定 Agent 配置。需要管理员权限。",
)
async def delete_agent(
    agent_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> None:
    """删除 Agent 配置（204，无响应体）。"""
    try:
        await service.delete_agent(agent_id)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
