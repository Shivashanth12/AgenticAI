"""Single local worker with database claims, heartbeat, fencing and safe restart recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import bind_request_context, configure_logging
from app.db.base import SessionFactory
from app.db.models import AuditEvent, RunStatus, TaskStatus, WorkflowRun, WorkflowTask
from app.orchestration.engine import WorkflowEngine


async def recover_expired() -> None:
    async with SessionFactory() as session:
        runs = list(
            await session.scalars(
                select(WorkflowRun)
                .where(
                    WorkflowRun.status == RunStatus.RUNNING,
                    (WorkflowRun.lease_until < datetime.now(UTC))
                    | WorkflowRun.lease_until.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for run in runs:
            run.generation += 1
            run.status, run.claim_token, run.lease_until = RunStatus.SAFE_STOPPED, None, None
            run.finished_at = datetime.now(UTC)
            run.last_error = "Worker lease expired; interrupted work requires explicit retry"
            await session.execute(
                update(WorkflowTask)
                .where(WorkflowTask.run_id == run.id, WorkflowTask.status == TaskStatus.RUNNING)
                .values(status=TaskStatus.FAILED, error_code="WORKER_INTERRUPTED")
            )
            session.add(
                AuditEvent(
                    run_id=run.id,
                    actor="worker",
                    action="workflow.recovered",
                    resource_type="workflow",
                    resource_id=str(run.id),
                    outcome="safe_stopped",
                    detail={"reason": "expired execution lease"},
                    correlation_id="restart-recovery",
                )
            )
        await session.commit()
    if runs:
        import docker

        client = docker.from_env(timeout=5)
        try:
            for run in runs:
                for container in await asyncio.to_thread(
                    client.containers.list, all=True, filters={"label": f"agentic.run={run.id}"}
                ):
                    await asyncio.to_thread(container.remove, force=True)
        finally:
            client.close()


async def execute_one(run_id: UUID) -> None:
    settings = get_settings()
    bind_request_context(str(run_id))
    async with SessionFactory() as session:
        engine = WorkflowEngine(session)
        execution = asyncio.create_task(engine.execute(run_id))
        try:
            while not execution.done():
                await asyncio.wait({execution}, timeout=2)
                if execution.done():
                    break
                if engine.token is None:
                    continue
                async with SessionFactory() as heartbeat:
                    result = await heartbeat.execute(
                        update(WorkflowRun)
                        .where(
                            WorkflowRun.id == run_id,
                            WorkflowRun.claim_token == engine.token,
                            WorkflowRun.generation == engine.generation,
                            WorkflowRun.status == RunStatus.RUNNING,
                        )
                        .values(
                            lease_until=datetime.now(UTC)
                            + timedelta(seconds=settings.worker_lease_seconds)
                        )
                    )
                    await heartbeat.commit()
                    if result.rowcount != 1:
                        execution.cancel()
            await execution
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    while True:
        await recover_expired()
        async with SessionFactory() as session:
            run_id = await session.scalar(
                select(WorkflowRun.id)
                .where(WorkflowRun.status == RunStatus.PENDING)
                .order_by(WorkflowRun.created_at)
                .limit(1)
            )
        if run_id:
            await execute_one(run_id)
        else:
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
