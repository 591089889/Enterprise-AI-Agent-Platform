"""管理员 API Router。

所有 /admin/* 路由均需要管理员权限（``Depends(require_admin)``）。
普通用户访问会收到 403，未登录用户收到 401。

职责边界
--------

  HTTP Request (GET /admin/users)
    ↓
  Admin Router (admin.py)      ← 本模块
    ↓  Depends(require_admin)  ← 认证 + 角色检查
    ↓  Depends(get_db)
    ↓  UserRepository(db)
    ↓  AdminService(repo)
    ↓
  HTTP 200 + PaginatedUsersResponse
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.dependencies import get_admin_service, require_admin
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.admin import (
    AdminUserRoleUpdate,
    DashboardStats,
    PaginatedUsersResponse,
)
from app.schemas.user import UserRead
from app.services.admin import AdminService

# ------------------------------------------------------------------
# APIRouter 实例
# ------------------------------------------------------------------
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


# ------------------------------------------------------------------
# 平台概览统计（Dashboard，Stage 18）
# ------------------------------------------------------------------
@router.get(
    "/dashboard",
    response_model=DashboardStats,
    tags=["dashboard"],
    summary="平台概览统计",
    description=(
        "返回平台核心资源数量：用户 / Agent / 知识库 / 文档 / 会话 / 消息。\n\n"
        "**权限**：需要管理员权限（未登录 401、普通用户 403）。\n\n"
        "**数据来源**：数据库实时 ``SELECT COUNT(*)``，不缓存、不估算、不伪造。"
    ),
)
async def get_dashboard(
    # 管理员认证 + 角色检查（普通用户 403）
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> DashboardStats:
    """获取平台概览统计（Dashboard 页面的唯一数据源）。"""
    stats = await service.get_dashboard_stats()
    return DashboardStats.model_validate(stats)


# ------------------------------------------------------------------
# 管理员获取用户列表
# ------------------------------------------------------------------
@router.get(
    "/users",
    response_model=PaginatedUsersResponse,
    summary="管理员获取用户列表",
    description="分页获取所有用户。需要管理员权限。",
)
async def list_users(
    # 管理员认证 + 角色检查
    admin: Annotated[User, Depends(require_admin)],
    # AdminService 依赖
    service: Annotated[AdminService, Depends(get_admin_service)],
    # 分页参数
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(
        default=20, ge=1, le=100, description="每页数量",
    ),
) -> PaginatedUsersResponse:
    """获取分页用户列表。

    只有管理员可以调用此接口。
    """
    users, total = await service.list_users(page=page, page_size=page_size)

    return PaginatedUsersResponse(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )


# ------------------------------------------------------------------
# 管理员查看任意用户
# ------------------------------------------------------------------
@router.get(
    "/users/{user_id}",
    response_model=UserRead,
    summary="管理员查看用户",
    description="查看指定用户的详细信息。需要管理员权限。",
)
async def get_user(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> UserRead:
    """管理员查看指定用户。

    如果用户不存在，AdminService 抛出 ResourceNotFoundException，
    Router 通过下面统一的 try/except 映射为 HTTP 404。
    """
    try:
        return await service.get_user(user_id)
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )


# ------------------------------------------------------------------
# 管理员修改用户角色
# ------------------------------------------------------------------
@router.patch(
    "/users/{user_id}/role",
    response_model=UserRead,
    summary="管理员修改用户角色",
    description="修改指定用户的角色（user 或 admin）。需要管理员权限。",
)
async def update_user_role(
    user_id: int,
    data: AdminUserRoleUpdate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> UserRead:
    """管理员修改用户角色。

    安全规则：
    - 管理员不能修改自己的角色。
    - 只能设置为 "user" 或 "admin"（由 Pydantic Literal 校验）。
    """
    try:
        return await service.update_user_role(
            user_id=user_id,
            role=data.role,
            admin_id=admin.id,
        )
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )


# ------------------------------------------------------------------
# 管理员删除用户
# ------------------------------------------------------------------
@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="管理员删除用户",
    description="删除指定用户。需要管理员权限。管理员不能删除自己。",
)
async def delete_user(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> None:
    """管理员删除用户。

    安全规则：管理员不能删除自己。
    """
    try:
        await service.delete_user(
            user_id=user_id,
            admin_id=admin.id,
        )
    except AppException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )
