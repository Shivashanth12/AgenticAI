"""Create the initial governed URL platform schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

link_status = sa.Enum("ACTIVE", "DISABLED", "DELETED", name="linkstatus")
run_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "WAITING_APPROVAL",
    "COMPLETED",
    "FAILED",
    "SAFE_STOPPED",
    "CANCELLED",
    name="runstatus",
)
task_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "WAITING_APPROVAL",
    "COMPLETED",
    "FAILED",
    "STALE",
    "CANCELLED",
    name="taskstatus",
)


def upgrade() -> None:
    op.create_table(
        "short_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(200)),
        sa.Column("status", link_status, nullable=False),
        sa.Column("redirect_type", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("click_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("alias"),
    )
    op.create_index("ix_short_links_alias", "short_links", ["alias"])
    op.create_index("ix_links_status_expiry", "short_links", ["status", "expires_at"])
    op.create_index("uq_links_alias_lower", "short_links", [sa.text("lower(alias)")], unique=True)
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scenario", sa.String(32), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("normalized_requirement", sa.JSON()),
        sa.Column("graph_version", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("requester", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "click_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("link_id", sa.Uuid(), sa.ForeignKey("short_links.id", ondelete="CASCADE")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("visitor_hash", sa.String(64)),
        sa.Column("referrer_domain", sa.String(255)),
        sa.Column("user_agent_class", sa.String(64)),
        sa.Column("request_id", sa.String(64), nullable=False),
    )
    op.create_index("ix_click_link_time", "click_events", ["link_id", "occurred_at"])
    op.create_table(
        "link_idempotency",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "link_id", sa.Uuid(), sa.ForeignKey("short_links.id", ondelete="CASCADE"), unique=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
        sa.Column("node_key", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(64), nullable=False),
        sa.Column("status", task_status, nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("output", sa.JSON()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "node_key"),
    )
    op.create_table(
        "task_dependencies",
        sa.Column(
            "predecessor_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "successor_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE")),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("reviewer", sa.String(255)),
        sa.Column("comment", sa.Text()),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    _create_evidence_tables()


def _create_evidence_tables() -> None:
    definitions = {
        "audit_events": [
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL")),
            sa.Column("actor", sa.String(255), nullable=False),
            sa.Column("action", sa.String(128), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.String(64), nullable=False),
            sa.Column("outcome", sa.String(32), nullable=False),
            sa.Column("detail", sa.JSON(), nullable=False),
            sa.Column("correlation_id", sa.String(64), nullable=False),
        ],
        "task_attempts": [
            sa.Column("task_id", sa.Uuid(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE")),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(64)),
            sa.Column("output", sa.JSON()),
            sa.UniqueConstraint("task_id", "attempt_number"),
        ],
        "workflow_checkpoints": [
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(128), nullable=False),
            sa.Column("state", sa.JSON(), nullable=False),
        ],
        "workflow_decisions": [
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
            sa.Column(
                "task_id", sa.Uuid(), sa.ForeignKey("workflow_tasks.id", ondelete="SET NULL")
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("decision_type", sa.String(64), nullable=False),
            sa.Column("decision", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text()),
            sa.Column("actor", sa.String(255), nullable=False),
        ],
        "policy_evaluations": [
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")),
            sa.Column(
                "task_id", sa.Uuid(), sa.ForeignKey("workflow_tasks.id", ondelete="SET NULL")
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("policy", sa.String(64), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("detail", sa.JSON(), nullable=False),
        ],
    }
    for name, columns in definitions.items():
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            *columns,
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    for table in (
        "policy_evaluations",
        "workflow_decisions",
        "workflow_checkpoints",
        "task_attempts",
        "audit_events",
        "artifacts",
        "approvals",
        "task_dependencies",
        "workflow_tasks",
        "link_idempotency",
        "click_events",
        "workflow_runs",
        "short_links",
    ):
        op.drop_table(table)
    task_status.drop(op.get_bind(), checkfirst=True)
    run_status.drop(op.get_bind(), checkfirst=True)
    link_status.drop(op.get_bind(), checkfirst=True)
