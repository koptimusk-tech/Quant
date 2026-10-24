"""agent messages of the alpha research cycle

Revision ID: 0003
Revises: 0002
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(length=32), nullable=False),
        sa.Column("receiver", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["hypothesis_id"], ["hypotheses.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["experiments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_agent_messages_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_agent_messages_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_agent_messages_subject_key"), ["subject_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_agent_messages_hypothesis_id"), ["hypothesis_id"], unique=False)


def downgrade() -> None:
    op.drop_table("agent_messages")
