"""Agent 配置 Repository 层。

职责边界
--------
只封装 ``agents`` 表的数据库访问，**不包含业务逻辑**：

- 不校验 provider 合法性（Service 层）；
- 不控制事务：只 ``flush()`` / ``refresh()``，
  ``commit()`` / ``rollback()`` 由 ``app.database.session.get_db`` 统一负责。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_tool import AgentTool


class AgentRepository:
    """Agent 配置数据仓库。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, agent: Agent) -> Agent:
        """创建 Agent 配置（flush + refresh，不 commit）。"""
        self.session.add(agent)
        await self.session.flush()
        await self.session.refresh(agent)
        return agent

    async def get_by_id(self, agent_id: int) -> Agent | None:
        """按 ID 查询 Agent 配置（不存在返回 None）。"""
        stmt = select(Agent).where(Agent.id == agent_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Agent]:
        """列出全部 Agent 配置。

        稳定排序：``created_at ASC, id ASC``（先创建的排在前面，
        与前端列表展示顺序一致；Agent 数量规模小，不做分页）。
        """
        stmt = select(Agent).order_by(Agent.created_at.asc(), Agent.id.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_all(self) -> int:
        """统计 Agent 总数（Dashboard 概览使用，Stage 18）。

        Returns:
            数据库中 agents 表的总行数。
        """
        stmt = select(func.count()).select_from(Agent)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Agent ↔ Tool 绑定（Stage 17）
    # ------------------------------------------------------------------
    async def list_tool_names(self, agent_id: int) -> list[str]:
        """列出某个 Agent 绑定的工具名（按名称排序，顺序稳定）。"""
        stmt = (
            select(AgentTool.tool_name)
            .where(AgentTool.agent_id == agent_id)
            .order_by(AgentTool.tool_name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_tool_names_for(
        self, agent_ids: list[int]
    ) -> dict[int, list[str]]:
        """批量列出多个 Agent 的工具名（一次查询，避免 N+1）。"""
        if not agent_ids:
            return {}
        stmt = (
            select(AgentTool.agent_id, AgentTool.tool_name)
            .where(AgentTool.agent_id.in_(agent_ids))
            .order_by(AgentTool.agent_id.asc(), AgentTool.tool_name.asc())
        )
        result = await self.session.execute(stmt)
        mapping: dict[int, list[str]] = {agent_id: [] for agent_id in agent_ids}
        for agent_id, tool_name in result.all():
            mapping.setdefault(agent_id, []).append(tool_name)
        return mapping

    async def set_tool_names(self, agent_id: int, tool_names: list[str]) -> None:
        """覆盖式设置某 Agent 的工具绑定（先删后插，不 commit）。

        ``tool_names`` 由 Service 层去重并校验（必须是已注册工具）。
        """
        existing = await self.session.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id)
        )
        for row in existing.scalars().all():
            await self.session.delete(row)

        # 去重并保持稳定顺序，避免唯一约束冲突
        for name in sorted({name for name in tool_names if name}):
            self.session.add(AgentTool(agent_id=agent_id, tool_name=name))

        await self.session.flush()

    async def update(self, agent: Agent, **kwargs: object) -> Agent:
        """部分更新 Agent 字段（flush + refresh，不 commit）。

        只更新 kwargs 中出现的字段；调用方应确保 ``agent`` 已绑定当前
        Session（通常由 :meth:`get_by_id` 查询得到）。
        """
        for key, value in kwargs.items():
            if hasattr(agent, key):
                setattr(agent, key, value)
        await self.session.flush()
        await self.session.refresh(agent)
        return agent

    async def delete(self, agent: Agent) -> None:
        """删除 Agent 配置（delete + flush，不 commit）。"""
        await self.session.delete(agent)
        await self.session.flush()
