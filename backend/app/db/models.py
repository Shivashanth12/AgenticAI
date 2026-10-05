from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class LinkStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SAFE_STOPPED = "safe_stopped"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class ShortLink(Base):
    __tablename__ = "short_links"
    __table_args__ = (
        Index("ix_links_status_expiry", "status", "expires_at"),
        Index("uq_links_alias_lower", func.lower(literal_column("alias")), unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    alias: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[LinkStatus] = mapped_column(Enum(LinkStatus), default=LinkStatus.ACTIVE)
    redirect_type: Mapped[int] = mapped_column(Integer, default=302)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    click_count: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClickEvent(Base):
    __tablename__ = "click_events"
    __table_args__ = (Index("ix_click_link_time", "link_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    link_id: Mapped[UUID] = mapped_column(ForeignKey("short_links.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    visitor_hash: Mapped[str | None] = mapped_column(String(64))
    referrer_domain: Mapped[str | None] = mapped_column(String(255))
    user_agent_class: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))


class LinkIdempotency(Base):
    __tablename__ = "link_idempotency"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    link_id: Mapped[UUID] = mapped_column(
        ForeignKey("short_links.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scenario: Mapped[str] = mapped_column(String(32))
    requirement: Mapped[str] = mapped_column(Text)
    normalized_requirement: Mapped[dict | None] = mapped_column(JsonType)
    graph_version: Mapped[str] = mapped_column(String(32), default="1.0")
    revision: Mapped[int] = mapped_column(default=1)
    generation: Mapped[int] = mapped_column(default=1)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.PENDING)
    risk_level: Mapped[str] = mapped_column(String(16), default="medium")
    provider: Mapped[str] = mapped_column(String(64))
    requester: Mapped[str] = mapped_column(String(255), default="local-requester")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    tasks: Mapped[list["WorkflowTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WorkflowTask(Base):
    __tablename__ = "workflow_tasks"
    __table_args__ = (UniqueConstraint("run_id", "node_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    node_key: Mapped[str] = mapped_column(String(64))
    node_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    attempt_count: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    output: Mapped[dict | None] = mapped_column(JsonType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run: Mapped[WorkflowRun] = relationship(back_populates="tasks")


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    predecessor_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    successor_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_tasks.id", ondelete="CASCADE"), primary_key=True
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_tasks.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(128))
    risk_level: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload_hash: Mapped[str] = mapped_column(String(64))
    reviewer: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_tasks.id", ondelete="CASCADE"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(128))
    state: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowDecision(Base):
    __tablename__ = "workflow_decisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_tasks.id", ondelete="SET NULL")
    )
    revision: Mapped[int] = mapped_column(Integer)
    decision_type: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_tasks.id", ondelete="SET NULL")
    )
    revision: Mapped[int] = mapped_column(Integer)
    policy: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
