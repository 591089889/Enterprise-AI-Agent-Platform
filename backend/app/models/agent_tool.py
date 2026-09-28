from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AgentTool(Base):
    """Agent ↔ Tool 关联表（Stage 17）。

    对应数据库中的 ``agent_tools`` 表。

    设计说明
    --------
    - 只保存**已经注册在代码级 ToolRegistry 中的工具名称**（如 ``calculator``、
      ``datetime``），**不保存任何可执行代码 / 脚本 / 动态导入信息**；
      因此不存在「管理员写入 Python 代码并被动态执行」的风险（无 eval/exec）。
    - 工具合法性由 Service 层对照 ``build_default_registry()`` 校验；
    - 删除 Agent 时级联删除其工具绑定（ON DELETE CASCADE）；
    - 同一 Agent 不允许重复绑定同一工具（唯一约束）。
    """

    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_name", name="uq_agent_tools_agent_tool"),
    )

    # 主键，自增
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 所属 Agent ID → agents.id（删除 Agent 时级联删除绑定）
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 已注册工具的名称（长度 50，非空）
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 创建时间：默认当前时间（数据库服务器时间）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )
