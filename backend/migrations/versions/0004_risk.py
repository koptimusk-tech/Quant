"""risk policies and risk manager decisions

Revision ID: 0004
Revises: 0003
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_policies",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("risk_policies", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_risk_policies_name"), ["name"], unique=False)
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("policy_id", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=96), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("observed", sa.Text(), nullable=True),
        sa.Column("limit_value", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("as_of", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["policy_id"], ["risk_policies.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["experiments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("risk_decisions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_risk_decisions_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_risk_decisions_rule"), ["rule"], unique=False)
        batch_op.create_index(batch_op.f("ix_risk_decisions_decision"), ["decision"], unique=False)


def downgrade() -> None:
    op.drop_table("risk_decisions")
    op.drop_table("risk_policies")
