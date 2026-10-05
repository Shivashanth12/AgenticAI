from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


def expiration(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expiration must include a timezone")
    value = value.astimezone(UTC)
    if value <= datetime.now(UTC):
        raise ValueError("Expiration must be in the future")
    return value


class LinkCreate(BaseModel):
    target_url: AnyHttpUrl
    custom_alias: str | None = Field(default=None, min_length=4, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    redirect_type: Literal[301, 302, 307] = 302

    _expiration = field_validator("expires_at")(expiration)

    @field_validator("custom_alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        if value and (not value.replace("-", "").replace("_", "").isalnum()):
            raise ValueError("Alias may contain only letters, numbers, hyphens, and underscores")
        return value

    @field_validator("target_url")
    @classmethod
    def validate_destination(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password:
            raise ValueError("Destination URLs cannot contain credentials")
        host = (value.host or "").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError("Local network destinations are not allowed")
        try:
            address = ip_address(host.strip("[]"))
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("Private or reserved network destinations are not allowed")
        return value


class LinkUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: Literal["active", "disabled"] | None = None
    expires_at: datetime | None = None
    version: int = Field(ge=1)

    @field_validator("expires_at")
    @classmethod
    def validate_expiration(cls, value: datetime | None) -> datetime | None:
        return expiration(value)

    @field_validator("status")
    @classmethod
    def non_null_status(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Status cannot be null")
        return value


class LinkView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    alias: str
    target_url: str
    title: str | None
    status: str
    redirect_type: int
    expires_at: datetime | None
    click_count: int
    version: int
    created_at: datetime


class WorkflowCreate(BaseModel):
    requirement: str = Field(min_length=10, max_length=10_000)
    scenario: Literal["greenfield", "brownfield", "ambiguous", "custom"] = "custom"
    risk_level: Literal["low", "medium", "high"] = "medium"


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="reviewer", min_length=2, max_length=255)
    comment: str | None = Field(default=None, max_length=2000)


class RequirementRevision(BaseModel):
    requirement: str = Field(min_length=10, max_length=10_000)
    reason: str = Field(min_length=3, max_length=1000)
    from_stage: str = Field(default="intake", min_length=2, max_length=64)


class RollbackRequest(BaseModel):
    checkpoint_id: UUID | None = None
    reason: str = Field(min_length=3, max_length=1000)


class WorkflowView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scenario: str
    requirement: str
    graph_version: str
    revision: int
    status: str
    risk_level: str
    provider: str
    requester: str
    created_at: datetime
    finished_at: datetime | None
    generation: int
    last_error: str | None


class WorkflowTaskView(BaseModel):
    id: UUID
    key: str
    type: str
    status: str
    risk_level: str
    attempt_count: int
    max_attempts: int
    error_code: str | None
    output: dict[str, Any] | None


class WorkflowDetailView(WorkflowView):
    tasks: list[WorkflowTaskView]


class WorkflowMetricsView(BaseModel):
    total_runs: int
    completed: int
    safe_stopped: int
    cancelled: int
    success_rate: float
    retry_count: int
    retry_frequency: float
    rollback_count: int
    rollback_frequency: float
    mttr_seconds: float | None
    average_latency_seconds: float | None
