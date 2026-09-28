"""add hashed_password to users

Revision ID: 002
Revises: 001
Create Date: 2026-08-10 21:57:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "hashed_password")
