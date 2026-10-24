"""simulated paper trading: sessions, signals, orders, fills, positions, ledger, equity, events

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_history", sa.JSON(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("criteria_hash", sa.String(length=64), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("data_refs", sa.JSON(), nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("policy_id", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=96), nullable=False),
        sa.Column("initial_capital", sa.Float(), nullable=False),
        sa.Column("account_currency", sa.String(length=8), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("peak_equity", sa.Float(), nullable=False),
        sa.Column("day_start_equity", sa.Float(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=True),
        sa.Column("cursor_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clock_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bars_processed", sa.Integer(), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("evaluation", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["risk_policies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("paper_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_sessions_status"), ["status"], unique=False
        )

    op.create_table(
        "paper_equity",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("fees", sa.Float(), nullable=False),
        sa.Column("holding_costs", sa.Float(), nullable=False),
        sa.Column("gross_exposure", sa.Float(), nullable=False),
        sa.Column("net_exposure", sa.Float(), nullable=False),
        sa.Column("margin_used", sa.Float(), nullable=False),
        sa.Column("drawdown", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "bar_ts", name="uq_paper_equity"),
    )
    with op.batch_alter_table("paper_equity", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_equity_session_id"), ["session_id"], unique=False
        )

    op.create_table(
        "paper_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sim_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("instrument", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("paper_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_events_category"), ["category"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_events_level"), ["level"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_events_session_id"), ["session_id"], unique=False
        )

    op.create_table(
        "paper_ledger",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("instrument", sa.String(length=64), nullable=True),
        sa.Column("ref_id", sa.String(length=32), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("paper_ledger", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_ledger_session_id"), ["session_id"], unique=False
        )

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instrument", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("lots", sa.Float(), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("tif_bars", sa.Integer(), nullable=True),
        sa.Column("delay_bars", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("triggered", sa.Boolean(), nullable=False),
        sa.Column("bars_active", sa.Integer(), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("filled_quantity", sa.Float(), nullable=False),
        sa.Column("avg_fill_price", sa.Float(), nullable=True),
        sa.Column("last_fill_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("slippage_cost", sa.Float(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
    )
    with op.batch_alter_table("paper_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_orders_instrument"), ["instrument"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_orders_session_id"), ["session_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_orders_status"), ["status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_orders_strategy_id"), ["strategy_id"], unique=False
        )

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instrument", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("fees", sa.Float(), nullable=False),
        sa.Column("holding_costs", sa.Float(), nullable=False),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("fx_rate", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "strategy_id", "instrument", name="uq_paper_position"
        ),
    )
    with op.batch_alter_table("paper_positions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_positions_session_id"), ["session_id"], unique=False
        )

    op.create_table(
        "paper_signals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "strategy_id", "bar_ts", name="uq_paper_signal"
        ),
    )
    with op.batch_alter_table("paper_signals", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_signals_session_id"), ["session_id"], unique=False
        )

    op.create_table(
        "paper_fills",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instrument", sa.String(length=64), nullable=False),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("fx_rate", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("spread_cost", sa.Float(), nullable=False),
        sa.Column("slippage_cost", sa.Float(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["paper_orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["paper_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "seq", name="uq_paper_fill_seq"),
    )
    with op.batch_alter_table("paper_fills", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_fills_order_id"), ["order_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_fills_session_id"), ["session_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_fills", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_fills_session_id"))
        batch_op.drop_index(batch_op.f("ix_paper_fills_order_id"))

    op.drop_table("paper_fills")
    with op.batch_alter_table("paper_signals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_signals_session_id"))

    op.drop_table("paper_signals")
    with op.batch_alter_table("paper_positions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_positions_session_id"))

    op.drop_table("paper_positions")
    with op.batch_alter_table("paper_orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_orders_strategy_id"))
        batch_op.drop_index(batch_op.f("ix_paper_orders_status"))
        batch_op.drop_index(batch_op.f("ix_paper_orders_session_id"))
        batch_op.drop_index(batch_op.f("ix_paper_orders_instrument"))

    op.drop_table("paper_orders")
    with op.batch_alter_table("paper_ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_ledger_session_id"))

    op.drop_table("paper_ledger")
    with op.batch_alter_table("paper_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_events_session_id"))
        batch_op.drop_index(batch_op.f("ix_paper_events_level"))
        batch_op.drop_index(batch_op.f("ix_paper_events_category"))

    op.drop_table("paper_events")
    with op.batch_alter_table("paper_equity", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_equity_session_id"))

    op.drop_table("paper_equity")
    with op.batch_alter_table("paper_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_sessions_status"))

    op.drop_table("paper_sessions")
