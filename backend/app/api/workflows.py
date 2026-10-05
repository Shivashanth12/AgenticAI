from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import PrincipalDependency, current_principal, require_role
from app.core.errors import NotFoundError
from app.db.base import get_session
from app.db.models import (
    Approval,
    Artifact,
    AuditEvent,
    PolicyEvaluation,
    TaskAttempt,
    TaskDependency,
    WorkflowCheckpoint,
    WorkflowDecision,
    WorkflowRun,
    WorkflowTask,
)
from app.orchestration.engine import WorkflowEngine
from app.schemas import (
    ApprovalDecision,
    RequirementRevision,
    RollbackRequest,
    WorkflowCreate,
    WorkflowDetailView,
    WorkflowMetricsView,
    WorkflowView,
)

router = APIRouter(
    prefix="/api/v1/workflows", tags=["workflows"], dependencies=[Depends(current_principal)]
)
Session = Annotated[AsyncSession, Depends(get_session)]


def run_view(run: WorkflowRun) -> dict:
    return {
        **WorkflowView.model_validate(run).model_dump(mode="json"),
        "tasks": [
            {
                "id": str(task.id),
                "key": task.node_key,
                "type": task.node_type,
                "status": task.status,
                "risk_level": task.risk_level,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "error_code": task.error_code,
                "output": task.output,
            }
            for task in run.tasks
        ],
    }


@router.post("", status_code=201, response_model=WorkflowDetailView)
async def create_workflow(
    payload: WorkflowCreate, session: Session, principal: PrincipalDependency
) -> dict:
    require_role(principal, "requester")
    engine = WorkflowEngine(session)
    run = await engine.create(payload, principal.actor)
    refreshed = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run.id).options(selectinload(WorkflowRun.tasks))
    )
    return run_view(refreshed)


@router.get("")
async def list_workflows(
    session: Session, limit: int = Query(30, ge=1, le=100)
) -> list[WorkflowView]:
    runs = await session.scalars(
        select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit)
    )
    return [WorkflowView.model_validate(run) for run in runs]


@router.get("/summary/metrics", response_model=WorkflowMetricsView)
async def workflow_metrics(session: Session) -> dict:
    runs = list(await session.scalars(select(WorkflowRun)))
    attempts = list(await session.scalars(select(TaskAttempt)))
    task_to_run = {task.id: task.run_id for task in await session.scalars(select(WorkflowTask))}
    decisions = list(await session.scalars(select(WorkflowDecision)))
    completed = [run for run in runs if run.status.value == "completed"]
    terminal = [
        run for run in runs if run.status.value in {"completed", "safe_stopped", "cancelled"}
    ]
    durations = []
    for run in terminal:
        if run.started_at and run.finished_at:
            start = (
                run.started_at.replace(tzinfo=UTC)
                if run.started_at.tzinfo is None
                else run.started_at
            )
            finish = (
                run.finished_at.replace(tzinfo=UTC)
                if run.finished_at.tzinfo is None
                else run.finished_at
            )
            durations.append(max((finish - start).total_seconds(), 0))
    recovery_durations = []
    for run in completed:
        failed = [
            item.started_at or item.created_at
            for item in attempts
            if item.status == "failed" and task_to_run.get(item.task_id) == run.id
        ]
        if failed and run.finished_at:
            failure = min(failed)
            failure = failure.replace(tzinfo=UTC) if failure.tzinfo is None else failure
            finish = (
                run.finished_at.replace(tzinfo=UTC)
                if run.finished_at.tzinfo is None
                else run.finished_at
            )
            recovery_durations.append(max((finish - failure).total_seconds(), 0))
    # A new revision is new work, not a retry. Failed primary -> fallback is an extra attempt.
    groups: dict[tuple, int] = {}
    for item in attempts:
        key = (item.task_id, item.revision)
        groups[key] = groups.get(key, 0) + 1
    retries = sum(max(count - 1, 0) for count in groups.values())
    rollbacks = sum(item.decision_type == "rollback" for item in decisions)
    return {
        "total_runs": len(runs),
        "completed": len(completed),
        "safe_stopped": sum(run.status.value == "safe_stopped" for run in runs),
        "cancelled": sum(run.status.value == "cancelled" for run in runs),
        "success_rate": round(len(completed) / len(terminal), 4) if terminal else 0,
        "retry_count": retries,
        "retry_frequency": round(retries / len(attempts), 4) if attempts else 0,
        "rollback_count": rollbacks,
        "rollback_frequency": round(rollbacks / len(runs), 4) if runs else 0,
        "mttr_seconds": round(sum(recovery_durations) / len(recovery_durations), 3)
        if recovery_durations
        else None,
        "average_latency_seconds": round(sum(durations) / len(durations), 3) if durations else None,
    }


@router.get("/{run_id}", response_model=WorkflowDetailView)
async def get_workflow(run_id: UUID, session: Session) -> dict:
    run = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.tasks))
    )
    if not run:
        raise NotFoundError("Workflow")
    return run_view(run)


@router.get("/{run_id}/graph")
async def get_graph(run_id: UUID, session: Session) -> dict:
    run = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.tasks))
    )
    if not run:
        raise NotFoundError("Workflow")
    task_by_id = {task.id: task for task in run.tasks}
    dependencies = list(
        await session.scalars(
            select(TaskDependency).where(TaskDependency.successor_id.in_(task_by_id))
        )
    )
    return {
        "nodes": [{"id": task.node_key, "status": task.status} for task in run.tasks],
        "edges": [
            {
                "source": task_by_id[item.predecessor_id].node_key,
                "target": task_by_id[item.successor_id].node_key,
            }
            for item in dependencies
        ],
    }


@router.get("/{run_id}/approvals")
async def list_approvals(run_id: UUID, session: Session) -> list[dict]:
    approvals = await session.scalars(
        select(Approval).where(Approval.run_id == run_id).order_by(Approval.requested_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "action": item.action,
            "risk_level": item.risk_level,
            "status": item.status,
            "reviewer": item.reviewer,
            "comment": item.comment,
            "requested_at": item.requested_at,
        }
        for item in approvals
    ]


@router.post("/{run_id}/approvals/{approval_id}/decision", response_model=WorkflowDetailView)
async def decide_approval(
    run_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    session: Session,
    principal: PrincipalDependency,
) -> dict:
    require_role(principal, "reviewer")
    engine = WorkflowEngine(session)
    payload = payload.model_copy(update={"reviewer": principal.actor})
    await engine.decide(run_id, approval_id, payload)
    refreshed = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.tasks))
    )
    return run_view(refreshed)


@router.get("/{run_id}/artifacts")
async def list_artifacts(run_id: UUID, session: Session) -> list[dict]:
    items = await session.scalars(
        select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
    )
    return [
        {
            "id": str(item.id),
            "kind": item.kind,
            "version": item.version,
            "content": item.content,
            "sha256": item.sha256,
            "status": item.status,
            "metadata": item.metadata_json,
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.get("/{run_id}/audit")
async def list_audit(run_id: UUID, session: Session) -> list[dict]:
    events = await session.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "action": item.action,
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "outcome": item.outcome,
            "detail": item.detail,
            "actor": item.actor,
            "correlation_id": item.correlation_id,
            "created_at": item.created_at,
        }
        for item in events
    ]


@router.get("/{run_id}/attempts")
async def list_attempts(run_id: UUID, session: Session) -> list[dict]:
    run = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.tasks))
    )
    if not run:
        raise NotFoundError("Workflow")
    task_names = {task.id: task.node_key for task in run.tasks}
    task_ids = list(task_names)
    items = (
        list(
            await session.scalars(
                select(TaskAttempt)
                .where(TaskAttempt.task_id.in_(task_ids))
                .order_by(TaskAttempt.created_at.desc())
            )
        )
        if task_ids
        else []
    )
    return [
        {
            "id": str(item.id),
            "task_id": str(item.task_id),
            "task": task_names[item.task_id],
            "attempt_number": item.attempt_number,
            "revision": item.revision,
            "provider": item.provider,
            "status": item.status,
            "duration_ms": item.duration_ms,
            "error_code": item.error_code,
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.get("/{run_id}/checkpoints")
async def list_checkpoints(run_id: UUID, session: Session) -> list[dict]:
    if not await session.get(WorkflowRun, run_id):
        raise NotFoundError("Workflow")
    items = await session.scalars(
        select(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.run_id == run_id)
        .order_by(WorkflowCheckpoint.created_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "revision": item.revision,
            "reason": item.reason,
            "state": item.state,
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.get("/{run_id}/decisions")
async def list_decisions(run_id: UUID, session: Session) -> list[dict]:
    if not await session.get(WorkflowRun, run_id):
        raise NotFoundError("Workflow")
    items = await session.scalars(
        select(WorkflowDecision)
        .where(WorkflowDecision.run_id == run_id)
        .order_by(WorkflowDecision.created_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "task_id": str(item.task_id) if item.task_id else None,
            "revision": item.revision,
            "type": item.decision_type,
            "decision": item.decision,
            "rationale": item.rationale,
            "actor": item.actor,
            "created_at": item.created_at,
        }
        for item in items
    ]


async def _refreshed_run(run_id: UUID, session: AsyncSession) -> dict:
    run = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.tasks))
    )
    if not run:
        raise NotFoundError("Workflow")
    return run_view(run)


@router.post("/{run_id}/cancel", response_model=WorkflowDetailView)
async def cancel_workflow(run_id: UUID, session: Session, principal: PrincipalDependency) -> dict:
    require_role(principal, "requester")
    await WorkflowEngine(session).cancel(run_id, principal.actor)
    return await _refreshed_run(run_id, session)


@router.post("/{run_id}/retry", response_model=WorkflowDetailView)
async def retry_workflow(run_id: UUID, session: Session, principal: PrincipalDependency) -> dict:
    require_role(principal, "requester")
    engine = WorkflowEngine(session)
    await engine.retry_failed(run_id, principal.actor)
    return await _refreshed_run(run_id, session)


@router.post("/{run_id}/requirements", response_model=WorkflowDetailView)
async def revise_requirement(
    run_id: UUID, payload: RequirementRevision, session: Session, principal: PrincipalDependency
) -> dict:
    require_role(principal, "requester")
    engine = WorkflowEngine(session)
    await engine.replan(run_id, payload, principal.actor)
    return await _refreshed_run(run_id, session)


@router.post("/{run_id}/rollback", response_model=WorkflowDetailView)
async def rollback_workflow(
    run_id: UUID, payload: RollbackRequest, session: Session, principal: PrincipalDependency
) -> dict:
    require_role(principal, "reviewer")
    engine = WorkflowEngine(session)
    await engine.rollback(run_id, payload, principal.actor)
    return await _refreshed_run(run_id, session)


@router.get("/{run_id}/policies")
async def list_policy_evaluations(run_id: UUID, session: Session) -> list[dict]:
    if not await session.get(WorkflowRun, run_id):
        raise NotFoundError("Workflow")
    items = await session.scalars(
        select(PolicyEvaluation)
        .where(PolicyEvaluation.run_id == run_id)
        .order_by(PolicyEvaluation.created_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "task_id": str(item.task_id) if item.task_id else None,
            "revision": item.revision,
            "policy": item.policy,
            "outcome": item.outcome,
            "detail": item.detail,
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.get("/{run_id}/bundle")
async def download_bundle(run_id: UUID, session: Session):
    import difflib
    import io
    import json
    import zipfile

    from fastapi import Response

    from app.core.errors import ConflictError
    from app.orchestration.candidates import digest
    from app.orchestration.tools import candidate_files

    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFoundError("Workflow")
    artifacts = list(
        await session.scalars(
            select(Artifact).where(Artifact.run_id == run_id, Artifact.status == "active")
        )
    )
    outputs = {a.kind: json.loads(a.content) for a in artifacts}
    implementation = outputs.get("implementation")
    if not implementation:
        raise ConflictError("A completed implementation is required for a review bundle")
    files = candidate_files(implementation)
    base = implementation.get("baseline_files", {})
    patch = "".join(
        line
        for path, content in files.items()
        for line in difflib.unified_diff(
            base.get(path, "").splitlines(True),
            content.splitlines(True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    manifest = {
        "workflow": str(run.id),
        "revision": run.revision,
        "status": run.status.value,
        "requirement": run.requirement,
        "candidate_sha256": digest(files),
        "baseline_sha256": digest(base),
        "artifacts": {a.kind: a.sha256 for a in artifacts},
        "validation": outputs.get("tests", {}).get("validation"),
        "release": outputs.get("release"),
        "limitations": "Review bundle; no host modification or deployment performed",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, content in files.items():
            bundle.writestr(f"candidate/{path}", content)
        for path, content in base.items():
            bundle.writestr(f"baseline/{path}", content)
        bundle.writestr(
            "candidate/test_generated.py", outputs.get("tests", {}).get("test_code", "")
        )
        for document in outputs.get("documentation", {}).get("documents", []):
            if isinstance(document, dict) and document.get("path") == "README.md":
                bundle.writestr("candidate/README.md", document.get("content", ""))
        validation = outputs.get("tests", {}).get("validation", {})
        checks = validation.get("checks", [])
        if checks and checks[-1].get("passed"):
            try:
                contract = json.loads(checks[-1].get("output", ""))
                if "openapi" in contract:
                    bundle.writestr("candidate/openapi.json", json.dumps(contract, indent=2))
            except (ValueError, TypeError):
                pass
        if "expiration" in implementation.get("features", []) and base:
            bundle.writestr("migration.sql", "ALTER TABLE links ADD COLUMN expires REAL;\n")
        bundle.writestr("change.patch", patch)
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        bundle.writestr("artifacts.json", json.dumps(outputs, indent=2))
        approvals = list(await session.scalars(select(Approval).where(Approval.run_id == run_id)))
        bundle.writestr(
            "approvals.json",
            json.dumps(
                [
                    {
                        "action": a.action,
                        "status": a.status,
                        "reviewer": a.reviewer,
                        "comment": a.comment,
                        "payload_hash": a.payload_hash,
                    }
                    for a in approvals
                ],
                indent=2,
            ),
        )
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="workflow-{run.id}-r{run.revision}.zip"'
        },
    )
