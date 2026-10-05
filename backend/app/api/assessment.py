"""Assessment evidence endpoints added without altering existing workflow APIs."""

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import PrincipalDependency, current_principal
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.base import get_session
from app.db.models import Artifact, WorkflowRun, WorkflowTask
from app.orchestration.brownfield import BrownfieldAnalyzer
from app.orchestration.quality import assess_artifact_quality

router = APIRouter(
    prefix="/api/v1/assessment",
    tags=["assessment"],
    dependencies=[Depends(current_principal)],
)
workflow_router = APIRouter(
    prefix="/api/v1/workflows",
    tags=["assessment"],
    dependencies=[Depends(current_principal)],
)
Session = Annotated[AsyncSession, Depends(get_session)]


class BrownfieldRequest(BaseModel):
    requirement: str = Field(min_length=10, max_length=10_000)


class ArtifactQualityRequest(BaseModel):
    implementation: dict[str, Any]
    test_code: str = ""
    documents: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/brownfield")
async def brownfield_evidence(payload: BrownfieldRequest, _: PrincipalDependency) -> dict[str, Any]:
    return BrownfieldAnalyzer(get_settings()).analyze(payload.requirement)


@router.post("/artifact-quality")
async def artifact_quality(
    payload: ArtifactQualityRequest, _: PrincipalDependency
) -> dict[str, Any]:
    return assess_artifact_quality(payload.implementation, payload.test_code, payload.documents)


@workflow_router.get("/{run_id}/assessment")
async def workflow_assessment(
    run_id: UUID, session: Session, _: PrincipalDependency
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFoundError("Workflow")
    tasks = list(await session.scalars(select(WorkflowTask).where(WorkflowTask.run_id == run_id)))
    artifacts = list(
        await session.scalars(
            select(Artifact).where(Artifact.run_id == run_id, Artifact.status == "active")
        )
    )
    outputs: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        try:
            outputs[artifact.kind] = json.loads(artifact.content)
        except json.JSONDecodeError:
            outputs[artifact.kind] = {}
    implementation = outputs.get("implementation", {})
    documents = outputs.get("documentation", {}).get("documents", [])
    quality = (
        assess_artifact_quality(
            implementation, outputs.get("tests", {}).get("test_code", ""), documents
        )
        if implementation
        else {
            "passed": False,
            "score": 0,
            "findings": [
                {
                    "severity": "error",
                    "code": "IMPLEMENTATION_MISSING",
                    "message": "No implementation artifact is available",
                }
            ],
            "files_checked": [],
        }
    )
    brownfield = (
        BrownfieldAnalyzer(get_settings()).analyze(run.requirement)
        if run.scenario == "brownfield"
        else None
    )
    return {
        "workflow_id": str(run.id),
        "scenario": run.scenario,
        "status": run.status.value,
        "revision": run.revision,
        "stage_evidence": {
            "total": len(tasks),
            "completed": sum(task.status.value == "completed" for task in tasks),
            "failed": sum(task.status.value == "failed" for task in tasks),
            "approval_gates": [task.node_key for task in tasks if task.node_type == "approval"],
        },
        "brownfield_analysis": brownfield,
        "artifact_quality": quality,
        "release_ready": quality.get("passed") is True and run.status.value == "completed",
        "limitations": [
            "Assessment evidence is read-only and does not modify host source files.",
            "Static quality analysis complements, but does not replace, sandbox execution.",
        ],
    }
