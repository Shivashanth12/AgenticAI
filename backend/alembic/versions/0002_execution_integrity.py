"""Add recoverable execution claims and revision-aware attempt evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs", sa.Column("generation", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("workflow_runs", sa.Column("claim_token", sa.String(64)))
    op.add_column("workflow_runs", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("workflow_runs", sa.Column("last_error", sa.Text()))
    op.add_column(
        "task_attempts", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("task_attempts", sa.Column("started_at", sa.DateTime(timezone=True)))
    # Repair legacy metadata-created indexes as well as preserving migration-created ones.
    op.drop_index("uq_links_alias_lower", table_name="short_links")
    op.create_index("uq_links_alias_lower", "short_links", [sa.text("lower(alias)")], unique=True)


def downgrade() -> None:
    op.drop_column("task_attempts", "started_at")
    op.drop_column("task_attempts", "revision")
    for name in ("last_error", "lease_until", "claim_token", "generation"):
        op.drop_column("workflow_runs", name)
