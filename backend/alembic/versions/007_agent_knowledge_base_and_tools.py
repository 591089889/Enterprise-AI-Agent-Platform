"""create agent_tools table and agents.knowledge_base_id

Revision ID: 007
Revises: 006
Create Date: 2026-09-25 19:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Stage 17：让 Agent 配置真正影响 Chat 执行。

    1. ``agents.knowledge_base_id``：Agent → Knowledge Base 绑定（0 或 1 个）；
       ``ON DELETE SET NULL`` —— 删除知识库时自动解除绑定，不产生悬空引用。
    2. ``agent_tools``：Agent ↔ 已注册工具的多对多绑定
       （只存工具名，不存可执行代码）。
    """
    # 1. Agent → Knowledge Base（可空 + SET NULL）
    op.add_column(
        "agents",
        sa.Column("knowledge_base_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_agents_knowledge_base_id"),
        "agents",
        ["knowledge_base_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_agents_knowledge_base_id_knowledge_bases"),
        "agents",
        "knowledge_bases",
        ["knowledge_base_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 2. Agent ↔ Tool 关联表
    op.create_table(
        "agent_tools",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_tools_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tools")),
        sa.UniqueConstraint(
            "agent_id", "tool_name", name="uq_agent_tools_agent_tool"
        ),
    )
    op.create_index(
        op.f("ix_agent_tools_agent_id"),
        "agent_tools",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_tools_agent_id"), table_name="agent_tools")
    op.drop_table("agent_tools")

    op.drop_constraint(
        op.f("fk_agents_knowledge_base_id_knowledge_bases"),
        "agents",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_agents_knowledge_base_id"), table_name="agents")
    op.drop_column("agents", "knowledge_base_id")
