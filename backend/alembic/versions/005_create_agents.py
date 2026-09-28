"""create agents table

Revision ID: 005
Revises: 004
Create Date: 2026-09-24 13:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 agents 表（Agent 配置管理，Stage 15）。

    与 ``app/models/agent.py`` 保持一致：
    id / name / description / system_prompt / provider / model /
    temperature / created_at / updated_at。
    """
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "description",
            sa.String(length=500),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "system_prompt",
            sa.String(length=4000),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.String(length=20),
            server_default="qwen",
            nullable=False,
        ),
        sa.Column(
            "model", sa.String(length=100), server_default="", nullable=False
        ),
        sa.Column(
            "temperature",
            sa.Float(),
            server_default=sa.text("0.7"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )


def downgrade() -> None:
    op.drop_table("agents")
