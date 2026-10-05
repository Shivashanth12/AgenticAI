import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from app.core.errors import ConflictError
from app.db.base import SessionFactory
from app.db.models import Approval, Artifact, RunStatus, WorkflowRun
from app.orchestration.engine import WorkflowEngine
from app.orchestration.providers import DeterministicProvider
from app.schemas import ApprovalDecision, RequirementRevision, RollbackRequest
from sqlalchemy import select


async def start(client, drive):
    response = await client.post(
        "/api/v1/workflows",
        json={"scenario": "greenfield", "requirement": "Build URL links with daily analytics"},
    )
    run_id = UUID(response.json()["id"])
    await drive(run_id)
    return run_id


async def test_partial_revision_reissues_design_approval(client, drive):
    run_id = await start(client, drive)
    async with SessionFactory() as session:
        engine = WorkflowEngine(session)
        await engine.replan(
            run_id,
            RequirementRevision(
                requirement="Build URL links with expiration",
                reason="New acceptance criteria",
                from_stage="implementation",
            ),
        )
    await drive(run_id)
    detail = (await client.get(f"/api/v1/workflows/{run_id}")).json()
    assert detail["status"] == "waiting_approval"
    approvals = (await client.get(f"/api/v1/workflows/{run_id}/approvals")).json()
    assert sum(a["status"] == "pending" for a in approvals) == 1
    assert any(a["status"] == "expired" for a in approvals)


async def test_rollback_restores_exact_artifact_set_and_context(client, drive):
    run_id = await start(client, drive)
    checkpoints = (await client.get(f"/api/v1/workflows/{run_id}/checkpoints")).json()
    initial = next(c for c in checkpoints if c["reason"] == "workflow_created")
    async with SessionFactory() as session:
        engine = WorkflowEngine(session)
        run = await engine.rollback(
            run_id,
            RollbackRequest(checkpoint_id=UUID(initial["id"]), reason="Restore initial checkpoint"),
        )
        active = list(
            await session.scalars(
                select(Artifact).where(Artifact.run_id == run_id, Artifact.status == "active")
            )
        )
        assert active == []
        assert run.normalized_requirement == initial["state"]["normalized_requirement"]
    await drive(run_id)
    assert (await client.get(f"/api/v1/workflows/{run_id}")).json()["status"] == "waiting_approval"


async def test_approval_is_bound_to_artifact_hash(client, drive):
    run_id = await start(client, drive)
    async with SessionFactory() as session:
        approval = await session.scalar(
            select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
        )
        artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run_id))
        artifact.sha256 = "0" * 64
        await session.commit()
        with pytest.raises(ConflictError, match="evidence changed"):
            await WorkflowEngine(session).decide(
                run_id,
                approval.id,
                ApprovalDecision(decision="approved", reviewer="local-reviewer"),
            )


async def test_cancel_fences_late_provider_results(client):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(DeterministicProvider):
        async def execute(self, node, requirement, context):
            entered.set()
            await release.wait()
            return await super().execute(node, requirement, context)

    response = await client.post("/api/v1/workflows", json={"requirement": "Build URL links"})
    run_id = UUID(response.json()["id"])
    async with SessionFactory() as first:
        execution = asyncio.create_task(WorkflowEngine(first, provider=Slow()).execute(run_id))
        await entered.wait()
        async with SessionFactory() as second:
            await WorkflowEngine(second).cancel(run_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
    async with SessionFactory() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.status == RunStatus.CANCELLED
        assert not list(await session.scalars(select(Artifact).where(Artifact.run_id == run_id)))


async def test_exclusive_execution_claim(client):
    response = await client.post("/api/v1/workflows", json={"requirement": "Build URL links"})
    run_id = UUID(response.json()["id"])
    async with SessionFactory() as session:
        run = await session.get(WorkflowRun, run_id)
        run.status, run.claim_token = RunStatus.RUNNING, "another-worker"
        await session.commit()
    async with SessionFactory() as session:
        result = await WorkflowEngine(session).execute(run_id)
        assert result.claim_token == "another-worker"
        assert not list(await session.scalars(select(Artifact).where(Artifact.run_id == run_id)))


async def test_management_reads_require_authentication(client):
    for path in ["/api/v1/links", "/api/v1/workflows", "/api/v1/activity"]:
        response = await client.get(path, headers={"Authorization": ""})
        assert response.status_code == 401


async def test_expiration_and_multiple_links(client):
    for alias in ["first-link", "second-link"]:
        assert (
            await client.post(
                "/api/v1/links", json={"target_url": "https://example.com", "custom_alias": alias}
            )
        ).status_code == 201
    future = datetime.now(UTC) + timedelta(hours=1)
    payload = {"target_url": "https://example.com", "custom_alias": "expires-link"}
    assert (
        await client.post(
            "/api/v1/links", json={**payload, "expires_at": future.replace(tzinfo=None).isoformat()}
        )
    ).status_code == 422
    created = await client.post("/api/v1/links", json={**payload, "expires_at": future.isoformat()})
    assert created.status_code == 201
    assert (await client.get("/expires-link", follow_redirects=False)).status_code == 302
    from app.db.models import ShortLink

    async with SessionFactory() as session:
        link = await session.get(ShortLink, UUID(created.json()["id"]))
        link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (await client.get("/expires-link", follow_redirects=False)).status_code == 410


async def test_concurrent_clicks_and_conditional_updates(client):
    created = (
        await client.post(
            "/api/v1/links",
            json={"target_url": "https://example.com", "custom_alias": "parallel-link"},
        )
    ).json()
    responses = await asyncio.gather(
        *(client.get("/parallel-link", follow_redirects=False) for _ in range(8))
    )
    assert all(r.status_code == 302 for r in responses)
    analytics = (await client.get(f"/api/v1/links/{created['id']}/analytics")).json()
    assert analytics["total_clicks"] == 8
    assert sum(d["clicks"] for d in analytics["daily"]) == 8
    responses = await asyncio.gather(
        *(
            client.patch(f"/api/v1/links/{created['id']}", json={"version": 1, "title": title})
            for title in ["First", "Second"]
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
