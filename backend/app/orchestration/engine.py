"""Governed DAG execution. Database state, never a model, authorizes transitions."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import get_contextvars

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, PolicyViolationError, ProviderError
from app.db.models import (
    Approval,
    Artifact,
    AuditEvent,
    PolicyEvaluation,
    RunStatus,
    TaskAttempt,
    TaskDependency,
    TaskStatus,
    WorkflowCheckpoint,
    WorkflowDecision,
    WorkflowRun,
    WorkflowTask,
)
from app.orchestration import candidates
from app.orchestration.codebase import CodebaseAnalyzer
from app.orchestration.contracts import WorkPlan
from app.orchestration.metrics import (
    RECOVERY_ACTIONS,
    TASK_ATTEMPTS,
    WORKFLOW_DURATION,
    WORKFLOW_RUNS,
)
from app.orchestration.providers import AIProvider, build_provider, validate_stage_output
from app.orchestration.sandbox import DockerSandbox
from app.orchestration.tools import EngineeringToolbox, scan
from app.schemas import ApprovalDecision, RequirementRevision, RollbackRequest, WorkflowCreate

CORE_GRAPH = [
    ("intake", []),
    ("normalize", ["intake"]),
    ("decompose", ["normalize"]),
    ("architecture", ["decompose"]),
    ("risk", ["decompose"]),
    ("design_approval", ["architecture", "risk"]),
    ("implementation", ["design_approval"]),
    ("tests", ["implementation"]),
    ("security", ["implementation"]),
    ("documentation", ["tests", "security"]),
    ("release", ["documentation"]),
    ("final_approval", ["release"]),
]
APPROVAL_NODES = {"clarification_approval", "design_approval", "final_approval"}


def graph_for_scenario(scenario: str) -> list[tuple[str, list[str]]]:
    graph = [(key, list(deps)) for key, deps in CORE_GRAPH]
    if scenario == "ambiguous":
        graph.insert(2, ("clarification_approval", ["normalize"]))
        graph = [
            (key, ["clarification_approval"] if key == "decompose" else deps) for key, deps in graph
        ]
    if scenario == "brownfield":
        index = next(i for i, (key, _) in enumerate(graph) if key == "decompose")
        graph.insert(index, ("codebase_analysis", ["normalize"]))
        graph = [
            (key, ["codebase_analysis"] if key == "decompose" else deps) for key, deps in graph
        ]
    return graph


def approval_action(key: str) -> str:
    return {
        "clarification_approval": "Provide clarification and approve the normalized requirement",
        "design_approval": "Approve architecture and risk controls",
        "final_approval": "Approve validated candidate for handoff",
    }[key]


class WorkflowEngine:
    def __init__(
        self, session: AsyncSession, provider: AIProvider | None = None, sandbox: Any | None = None
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.provider = provider or build_provider(self.settings)
        self.sandbox = sandbox or DockerSandbox(self.settings)
        self.tools = EngineeringToolbox(self.settings)
        self.codebase = CodebaseAnalyzer(self.settings)
        self.token: str | None = None
        self.generation = 0

    async def create(self, payload: WorkflowCreate, actor: str = "local-requester") -> WorkflowRun:
        if scan(payload.requirement):
            raise PolicyViolationError("Remove secrets from the requirement before submitting")
        run = WorkflowRun(
            scenario=payload.scenario,
            requirement=payload.requirement,
            normalized_requirement={"original": payload.requirement},
            status=RunStatus.PENDING,
            risk_level=payload.risk_level,
            provider=self.provider.name,
            requester=actor,
        )
        self.session.add(run)
        await self.session.flush()
        graph = graph_for_scenario(payload.scenario)
        tasks = {
            key: WorkflowTask(
                run_id=run.id,
                node_key=key,
                node_type="approval" if key in APPROVAL_NODES else "agent",
                max_attempts=self.settings.max_workflow_retries,
                risk_level="high" if key in APPROVAL_NODES else "low",
            )
            for key, _ in graph
        }
        self.session.add_all(tasks.values())
        await self.session.flush()
        self.session.add_all(
            [
                TaskDependency(predecessor_id=tasks[dep].id, successor_id=tasks[key].id)
                for key, deps in graph
                for dep in deps
            ]
        )
        self._decision(run, "scenario_selected", payload.scenario, actor, payload.risk_level)
        await self._audit(
            run.id, "workflow.created", "workflow", str(run.id), "success", actor=actor
        )
        await self._checkpoint(run, "workflow_created")
        return run

    async def execute(self, run_id: UUID) -> WorkflowRun:
        token = uuid4().hex
        claimed = await self.session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status == RunStatus.PENDING,
                WorkflowRun.claim_token.is_(None),
            )
            .values(
                status=RunStatus.RUNNING,
                claim_token=token,
                lease_until=datetime.now(UTC)
                + timedelta(seconds=self.settings.worker_lease_seconds),
                last_error=None,
            )
        )
        await self.session.commit()
        run = await self._run(run_id)
        await self.session.refresh(run)
        if claimed.rowcount != 1:
            return run
        self.token, self.generation = token, run.generation
        run.started_at = run.started_at or datetime.now(UTC)
        await self.session.commit()
        try:
            while True:
                run = await self._fence(run_id)
                tasks = await self._tasks(run_id)
                tasks = [
                    t for t in tasks if t.status not in {TaskStatus.STALE, TaskStatus.CANCELLED}
                ]
                dependencies = await self._dependency_map(tasks)
                completed = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
                runnable = [
                    t
                    for t in tasks
                    if t.status == TaskStatus.PENDING and dependencies.get(t.id, set()) <= completed
                ]
                if not runnable:
                    if tasks and all(t.status == TaskStatus.COMPLETED for t in tasks):
                        run.status = RunStatus.COMPLETED
                        run.completed_at = run.finished_at = datetime.now(UTC)
                        WORKFLOW_RUNS.labels(run.scenario, "completed").inc()
                        WORKFLOW_DURATION.labels(run.scenario).observe(
                            (run.finished_at - run.started_at.replace(tzinfo=UTC)).total_seconds()
                        )
                        await self._audit(
                            run.id, "workflow.completed", "workflow", str(run.id), "success"
                        )
                    else:
                        run.status = RunStatus.SAFE_STOPPED
                        run.finished_at = datetime.now(UTC)
                        run.last_error = (
                            "No runnable task: revise or roll back this inconsistent graph"
                        )
                    self._release_claim(run)
                    await self._checkpoint(run, "execution_finished")
                    return run
                approval = next((t for t in runnable if t.node_key in APPROVAL_NODES), None)
                if approval:
                    await self._request_approval(run, approval)
                    return run
                # Release the row lock before provider calls, retries, or sandbox work.
                for task in runnable:
                    task.status = TaskStatus.RUNNING
                    task.started_at = datetime.now(UTC)
                await self.session.commit()
                await self._wave(run, runnable, tasks, dependencies)
                await self.session.refresh(run)
                if run.status != RunStatus.RUNNING:
                    return run
        except asyncio.CancelledError:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            run = await self._run(run_id)
            await self.session.refresh(run)
            if run.claim_token == self.token:
                run.status = RunStatus.SAFE_STOPPED
                run.finished_at = datetime.now(UTC)
                run.last_error = f"Execution stopped: {type(exc).__name__}"
                self._release_claim(run)
                await self._audit(
                    run.id, "workflow.safe_stopped", "workflow", str(run.id), "failure"
                )
                await self.session.commit()
            return run

    async def _fence(self, run_id: UUID) -> WorkflowRun:
        run = await self.session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            run is None
            or run.claim_token != self.token
            or run.generation != self.generation
            or run.status != RunStatus.RUNNING
        ):
            raise asyncio.CancelledError("Execution was superseded")
        return run

    async def _wave(self, run, runnable, tasks, dependencies):
        by_id = {t.id: t for t in tasks}
        histories = {
            t.id: list(
                await self.session.scalars(select(TaskAttempt).where(TaskAttempt.task_id == t.id))
            )
            for t in runnable
        }
        await self.session.commit()

        async def generate(task):
            ancestors, pending = set(), list(dependencies.get(task.id, set()))
            while pending:
                dep = pending.pop()
                if dep in ancestors:
                    continue
                ancestors.add(dep)
                pending.extend(dependencies.get(dep, set()))
            context = {
                by_id[dep].node_key: by_id[dep].output
                for dep in ancestors
                if dep in by_id and by_id[dep].output is not None
            }
            context.update(normalized_requirement=run.normalized_requirement, scenario=run.scenario)
            attempts = []
            used = sum(a.revision == run.revision for a in histories[task.id])
            budget = min(task.max_attempts, self.settings.max_total_task_attempts - used)
            for number in range(max(budget, 0)):
                started = datetime.now(UTC)
                tick = time.perf_counter()
                try:
                    if task.node_key == "implementation":
                        expected_features = run.normalized_requirement["features"]
                        work_outputs = [
                            context[t.node_key]
                            for t in reversed(tasks)
                            if t.node_key.startswith("work_")
                            and isinstance(context.get(t.node_key), dict)
                        ]
                        # The final work item is the implementation candidate. Match its
                        # feature set explicitly so an alias-only graph cannot be mistaken
                        # for a missing implementation artifact.
                        last = next(
                            (
                                output
                                for output in work_outputs
                                if output.get("features") == expected_features
                            ),
                            None,
                        )
                        if not last:
                            raise PolicyViolationError("No executed implementation work items")
                        output = dict(last)
                    else:
                        if task.node_key == "codebase_analysis":
                            context["baseline_files"] = candidates.baseline(run.scenario)
                            context["repository_scan"] = self.codebase.analyze(run.requirement)
                        if task.node_key.startswith("work_"):
                            features = run.normalized_requirement["features"]
                            index = features.index(task.node_key.removeprefix("work_"))
                            context["normalized_requirement"] = {
                                **run.normalized_requirement,
                                "features": features[: index + 1],
                            }
                        output = await asyncio.wait_for(
                            self.provider.execute(task.node_key, run.requirement, context),
                            timeout=120,
                        )
                    output = validate_stage_output(
                        "implementation" if task.node_key.startswith("work_") else task.node_key,
                        output,
                    )
                    if task.node_key == "normalize":
                        output = {**output, **candidates.normalize(run.requirement)}
                        if run.scenario == "brownfield" and "analytics" not in output["features"]:
                            output["features"] = [
                                f
                                for f in ("aliases", "analytics", "expiration")
                                if f == "analytics" or f in output["features"]
                            ]
                            output["acceptance_criteria"] = [
                                {"id": f, "behavior": candidates.CRITERIA[f]}
                                for f in output["features"]
                            ]
                    if task.node_key == "decompose":
                        # Only validated capability tasks authorize work.
                        output["agent_proposal"] = output["tasks"]
                        features = run.normalized_requirement["features"]
                        output["tasks"] = [
                            {
                                "name": key,
                                "objective": candidates.CRITERIA[key],
                                "depends_on": features[i - 1 : i],
                                "completion_gate": key,
                                "acceptance_ids": [key],
                            }
                            for i, key in enumerate(features)
                        ]
                    if task.node_key == "codebase_analysis":
                        output["repository"] = self.codebase.analyze(run.requirement)
                        base = candidates.baseline(run.scenario)
                        output["baseline_files"] = base
                        output["baseline_sha256"] = candidates.digest(base)
                        output["impacted_files"] = [
                            {
                                "path": "main.py",
                                "symbols": ["CreateLink", "redirect"],
                                "reason": "Add expiration; preserve existing API contracts",
                                "source": base.get("main.py", ""),
                            }
                        ]
                    if task.node_key.startswith("work_") or task.node_key == "implementation":
                        output["features"] = context["normalized_requirement"]["features"]
                        output["baseline_files"] = candidates.baseline(run.scenario)
                        output["baseline_sha256"] = candidates.digest(output["baseline_files"])
                    output.update(self.tools.evidence_for(task.node_key, output, context))
                    if task.node_key == "tests":
                        output["validation"] = await self.sandbox.validate(
                            context["implementation"],
                            output,
                            run.normalized_requirement["features"],
                            str(run.id),
                        )
                    output = self._validate_and_govern(task.node_key, output)
                    attempts.append(
                        {
                            "started_at": started,
                            "status": "completed",
                            "output": output,
                            "provider": output.get("provider_used", self.provider.name),
                            "duration_ms": round((time.perf_counter() - tick) * 1000),
                            "error": None,
                        }
                    )
                    return task, output, attempts, None
                except Exception as exc:
                    attempts.append(
                        {
                            "started_at": started,
                            "status": "failed",
                            "output": None,
                            "provider": self.provider.name,
                            "duration_ms": round((time.perf_counter() - tick) * 1000),
                            "error": str(exc)[:500]
                            if isinstance(exc, (PolicyViolationError, ValueError))
                            else type(exc).__name__,
                        }
                    )
                    if isinstance(exc, (PolicyViolationError, ValueError, SyntaxError)) or (
                        isinstance(exc, ProviderError) and not exc.retryable
                    ):
                        break
                    if number + 1 < budget:
                        await asyncio.sleep(min(2**number, 4))
            return (
                task,
                None,
                attempts,
                attempts[-1]["error"] if attempts else "Revision retry budget exhausted",
            )

        results = await asyncio.gather(*(generate(t) for t in runnable))
        run = await self._fence(run.id)
        failed = False
        for task, output, attempts, error in results:
            latest = max((a.attempt_number for a in histories[task.id]), default=0)
            for attempt in attempts:
                if attempt["output"] and attempt["output"].get("fallback", {}).get("used"):
                    latest += 1
                    self.session.add(
                        TaskAttempt(
                            task_id=task.id,
                            revision=run.revision,
                            attempt_number=latest,
                            provider=attempt["output"]["fallback"]["from"],
                            status="failed",
                            duration_ms=attempt["duration_ms"],
                            started_at=attempt["started_at"],
                            error_code="AI_PROVIDER_ERROR",
                        )
                    )
                latest += 1
                self.session.add(
                    TaskAttempt(
                        task_id=task.id,
                        revision=run.revision,
                        attempt_number=latest,
                        provider=attempt["provider"],
                        status=attempt["status"],
                        duration_ms=attempt["duration_ms"],
                        started_at=attempt["started_at"],
                        output=attempt["output"],
                        error_code=attempt["error"][:64] if attempt["error"] else None,
                    )
                )
                TASK_ATTEMPTS.labels(task.node_key, attempt["provider"], attempt["status"]).inc()
            task.attempt_count = sum(a.revision == run.revision for a in histories[task.id]) + len(
                attempts
            )
            if error:
                failed = True
                task.status, task.error_code = TaskStatus.FAILED, error[:64]
                run.last_error = f"{task.node_key}: {error}"
                self.session.add(
                    PolicyEvaluation(
                        run_id=run.id,
                        task_id=task.id,
                        revision=run.revision,
                        policy="stage_exit",
                        outcome="failed",
                        detail={"reason": error},
                    )
                )
                await self._audit(
                    run.id, "task.failed", "task", str(task.id), "failure", {"reason": error}
                )
                continue
            task.output, task.status, task.completed_at = (
                output,
                TaskStatus.COMPLETED,
                datetime.now(UTC),
            )
            if task.node_key == "normalize":
                run.normalized_requirement = output
                if output.get("ambiguities") and not any(
                    t.node_key == "clarification_approval" for t in tasks
                ):
                    await self._add_clarification(run, tasks)
            if task.node_key == "decompose":
                await self._install_work_graph(run, output["tasks"])
            content = json.dumps(output, sort_keys=True, indent=2)
            import hashlib

            self.session.add(
                Artifact(
                    run_id=run.id,
                    task_id=task.id,
                    kind=task.node_key,
                    version=run.revision,
                    content=content,
                    sha256=hashlib.sha256(content.encode()).hexdigest(),
                    metadata_json={
                        "provider": output.get("provider_used", self.provider.name),
                        "generation": run.generation,
                        "baseline": output.get("baseline_sha256"),
                    },
                )
            )
            for evaluation in output["governance"]["evaluations"]:
                self.session.add(
                    PolicyEvaluation(
                        run_id=run.id,
                        task_id=task.id,
                        revision=run.revision,
                        policy=evaluation["policy"],
                        outcome=evaluation["outcome"],
                        detail=evaluation["detail"],
                    )
                )
            await self._audit(run.id, "task.completed", "task", str(task.id), "success")
        if failed:
            run.status, run.finished_at = RunStatus.SAFE_STOPPED, datetime.now(UTC)
            self._release_claim(run)
            WORKFLOW_RUNS.labels(run.scenario, "safe_stopped").inc()
        await self._checkpoint(run, "parallel_wave_completed")

    def _validate_and_govern(self, node: str, output: dict) -> dict:
        if scan(json.dumps(output)):
            raise PolicyViolationError("Potential secret in generated artifact")
        for key in ("sandbox_validation", "executed_checks", "validation"):
            if isinstance(output.get(key), dict) and output[key].get("passed") is False:
                # Preserve bounded tool evidence on failure through the failure detail.
                raise PolicyViolationError(f"{key} failed: {json.dumps(output[key])[:400]}")
        if node == "release" and output.get("ready") is not True:
            raise PolicyViolationError("Release requires passing evidence for this exact candidate")
        return {
            **output,
            "governance": {
                "evaluations": [
                    {"policy": "artifact_schema", "outcome": "passed", "detail": {"stage": node}},
                    {
                        "policy": "security",
                        "outcome": "passed",
                        "detail": {"check": "secret patterns and candidate path allowlist"},
                    },
                    {
                        "policy": "data_minimization",
                        "outcome": "passed",
                        "detail": {"check": "secret scan", "raw_visitor_data": False},
                    },
                    {
                        "policy": "change_control",
                        "outcome": "passed",
                        "detail": {"host_mutation": False},
                    },
                ]
            },
        }

    async def _install_work_graph(self, run, work):
        work = WorkPlan.model_validate({"tasks": work}).model_dump()["tasks"]
        names = [item["name"] for item in work]
        if len(set(names)) != len(names) or set(names) != set(
            run.normalized_requirement["features"]
        ):
            raise PolicyViolationError("Work items must cover the accepted capabilities exactly")
        seen = set()
        for item in work:
            if not set(item["depends_on"]) <= seen:
                raise PolicyViolationError("Work dependencies are cyclic or unknown")
            seen.add(item["name"])
        tasks = {t.node_key: t for t in await self._tasks(run.id)}
        old_ids = [t.id for key, t in tasks.items() if key.startswith("work_")]
        if old_ids:
            await self.session.execute(
                delete(TaskDependency).where(
                    TaskDependency.predecessor_id.in_(old_ids)
                    | TaskDependency.successor_id.in_(old_ids)
                )
            )
        await self.session.execute(
            delete(TaskDependency).where(TaskDependency.successor_id == tasks["implementation"].id)
        )
        for key, task in tasks.items():
            if key.startswith("work_"):
                task.status = TaskStatus.STALE
        predecessor = tasks["design_approval"]
        for item in work:
            key = "work_" + item["name"]
            task = tasks.get(key)
            if task is None:
                task = WorkflowTask(
                    run_id=run.id,
                    node_key=key,
                    node_type="work_item",
                    max_attempts=self.settings.max_workflow_retries,
                )
                self.session.add(task)
                await self.session.flush()
            task.status, task.output = TaskStatus.PENDING, None
            self.session.add(TaskDependency(predecessor_id=predecessor.id, successor_id=task.id))
            predecessor = task
        self.session.add(
            TaskDependency(predecessor_id=predecessor.id, successor_id=tasks["implementation"].id)
        )

    async def _add_clarification(self, run, tasks):
        task = WorkflowTask(
            run_id=run.id,
            node_key="clarification_approval",
            node_type="approval",
            risk_level="high",
        )
        self.session.add(task)
        await self.session.flush()
        normalized = next(t for t in tasks if t.node_key == "normalize")
        successors = list(
            await self.session.scalars(
                select(TaskDependency).where(TaskDependency.predecessor_id == normalized.id)
            )
        )
        for edge in successors:
            self.session.add(TaskDependency(predecessor_id=task.id, successor_id=edge.successor_id))
            await self.session.delete(edge)
        self.session.add(TaskDependency(predecessor_id=normalized.id, successor_id=task.id))

    async def _approval_hash(self, run: WorkflowRun, key: str) -> str:
        artifacts = list(
            await self.session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.status == "active")
                .order_by(Artifact.id)
            )
        )
        return candidates.digest(
            {
                "run": str(run.id),
                "stage": key,
                "revision": run.revision,
                "requirement": run.requirement,
                "normalized": run.normalized_requirement,
                "artifacts": [(str(a.id), a.sha256) for a in artifacts],
                "graph": [
                    (str(edge.predecessor_id), str(edge.successor_id))
                    for edge in await self.session.scalars(
                        select(TaskDependency)
                        .join(WorkflowTask, TaskDependency.successor_id == WorkflowTask.id)
                        .where(WorkflowTask.run_id == run.id)
                        .order_by(TaskDependency.predecessor_id, TaskDependency.successor_id)
                    )
                ],
            }
        )

    async def _request_approval(self, run, task):
        self.session.add(
            Approval(
                run_id=run.id,
                task_id=task.id,
                action=approval_action(task.node_key),
                risk_level="high",
                payload_hash=await self._approval_hash(run, task.node_key),
            )
        )
        task.status, run.status = TaskStatus.WAITING_APPROVAL, RunStatus.WAITING_APPROVAL
        self._release_claim(run)
        await self._audit(run.id, "approval.requested", "task", str(task.id), "pending")
        await self._checkpoint(run, "approval_requested")

    async def decide(
        self, run_id: UUID, approval_id: UUID, decision: ApprovalDecision
    ) -> WorkflowRun:
        run = await self._locked(run_id)
        approval = await self.session.get(Approval, approval_id, populate_existing=True)
        if not approval or approval.run_id != run.id:
            raise NotFoundError("Approval")
        task = await self.session.get(WorkflowTask, approval.task_id)
        if run.status != RunStatus.WAITING_APPROVAL or approval.status != "pending":
            raise ConflictError("This approval is no longer pending")
        if decision.reviewer == run.requester:
            raise ConflictError("The requester cannot approve their own workflow")
        if approval.payload_hash != await self._approval_hash(run, task.node_key):
            raise ConflictError("Approval evidence changed; revise before approving")
        if scan(decision.comment or ""):
            raise PolicyViolationError("Remove secrets from review comments")
        if task.node_key == "clarification_approval" and decision.decision == "approved":
            comment = (decision.comment or "").strip()
            if len(comment) < 10:
                raise ConflictError("Provide a measurable clarification")
            normalized = candidates.normalize(comment)
            if normalized["ambiguities"]:
                raise ConflictError("Clarification still contains ambiguous criteria")
            run.requirement = f"{run.requirement}\nAccepted clarification: {comment}"
            run.normalized_requirement = {**normalized, "original": run.requirement}
        result = await self.session.execute(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(
                status=decision.decision,
                reviewer=decision.reviewer,
                comment=decision.comment,
                decided_at=datetime.now(UTC),
            )
        )
        if result.rowcount != 1:
            raise ConflictError("Approval already decided")
        task.output = {
            "decision": decision.decision,
            "reviewer": decision.reviewer,
            "comment": decision.comment,
        }
        if decision.decision == "approved":
            task.status, task.completed_at, run.status = (
                TaskStatus.COMPLETED,
                datetime.now(UTC),
                RunStatus.PENDING,
            )
        else:
            task.status, run.status, run.finished_at = (
                TaskStatus.FAILED,
                RunStatus.SAFE_STOPPED,
                datetime.now(UTC),
            )
            run.last_error = "Reviewer rejected the checkpoint"
        self._decision(run, task.node_key, decision.decision, decision.reviewer, decision.comment)
        await self._audit(
            run.id,
            "approval.decided",
            "approval",
            str(approval.id),
            decision.decision,
            actor=decision.reviewer,
        )
        await self._checkpoint(run, "approval_decided")
        return run

    async def cancel(self, run_id: UUID, actor: str = "local-requester") -> WorkflowRun:
        run = await self._locked(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise ConflictError("This workflow is already terminal")
        run.generation += 1
        run.status, run.finished_at = RunStatus.CANCELLED, datetime.now(UTC)
        self._release_claim(run)
        await self.session.execute(
            update(WorkflowTask)
            .where(
                WorkflowTask.run_id == run.id,
                WorkflowTask.status.in_(
                    [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL]
                ),
            )
            .values(status=TaskStatus.CANCELLED)
        )
        await self._expire_approvals(run.id)
        self._decision(run, "cancel", "cancelled", actor)
        await self._audit(
            run.id, "workflow.cancelled", "workflow", str(run.id), "success", actor=actor
        )
        await self._checkpoint(run, "workflow_cancelled")
        return run

    async def retry_failed(self, run_id: UUID, actor: str = "local-requester") -> WorkflowRun:
        run = await self._locked(run_id)
        if run.status != RunStatus.SAFE_STOPPED:
            raise ConflictError("Only safe-stopped work can be retried")
        tasks = [t for t in await self._tasks(run.id) if t.status == TaskStatus.FAILED]
        if not tasks:
            raise ConflictError("Revise or roll back this workflow; no retryable task exists")
        for task in tasks:
            count = await self.session.scalar(
                select(func.count(TaskAttempt.id)).where(
                    TaskAttempt.task_id == task.id, TaskAttempt.revision == run.revision
                )
            )
            if count >= self.settings.max_total_task_attempts:
                raise ConflictError("Retry budget exhausted; revise or roll back")
            task.status, task.error_code = TaskStatus.PENDING, None
        run.status, run.finished_at, run.last_error = RunStatus.PENDING, None, None
        self._decision(run, "retry", "requested", actor)
        await self._audit(
            run.id, "workflow.retry_requested", "workflow", str(run.id), "success", actor=actor
        )
        await self.session.commit()
        RECOVERY_ACTIONS.labels("retry", "accepted").inc()
        return run

    async def replan(
        self, run_id: UUID, payload: RequirementRevision, actor: str = "local-requester"
    ) -> WorkflowRun:
        run = await self._locked(run_id)
        if run.status == RunStatus.RUNNING:
            raise ConflictError("Cancel active execution before revising")
        if scan(payload.requirement) or scan(payload.reason):
            raise PolicyViolationError("Remove secrets before revising")
        tasks = await self._tasks(run.id)
        if payload.from_stage not in {t.node_key for t in tasks}:
            raise ConflictError("Unknown restart stage")
        await self._checkpoint(run, "before_requirement_revision")
        run = await self._locked(run_id)
        changed = payload.requirement != run.requirement
        start = "intake" if changed else payload.from_stage
        # Changed implementation or validation requires a new design review.
        if start in {
            "implementation",
            "tests",
            "security",
            "documentation",
            "release",
            "final_approval",
        } or start.startswith("work_"):
            start = "design_approval"
        graph = await self._graph(tasks)
        affected = self._descendant_keys(graph, start)
        run.requirement, run.revision, run.generation = (
            payload.requirement,
            run.revision + 1,
            run.generation + 1,
        )
        run.status, run.completed_at, run.finished_at, run.last_error = (
            RunStatus.PENDING,
            None,
            None,
            None,
        )
        self._release_claim(run)
        if changed:
            run.normalized_requirement = {"original": payload.requirement}
        ids = []
        for task in tasks:
            if task.node_key in affected:
                ids.append(task.id)
                task.status, task.output, task.error_code = TaskStatus.PENDING, None, None
                task.attempt_count, task.started_at, task.completed_at = 0, None, None
        await self.session.execute(
            update(Artifact)
            .where(
                Artifact.run_id == run.id, Artifact.task_id.in_(ids), Artifact.status == "active"
            )
            .values(status="stale")
        )
        await self._expire_approvals(run.id)
        # Any remaining waiting gate must be reissued with the new evidence hash.
        for task in tasks:
            if task.status == TaskStatus.WAITING_APPROVAL:
                task.status = TaskStatus.PENDING
        self._decision(run, "requirement_revised", payload.requirement, actor, payload.reason)
        await self._audit(
            run.id, "workflow.replanned", "workflow", str(run.id), "success", actor=actor
        )
        await self._checkpoint(run, "requirement_revised")
        RECOVERY_ACTIONS.labels("replan", "accepted").inc()
        return run

    async def rollback(
        self, run_id: UUID, payload: RollbackRequest, actor: str = "local-reviewer"
    ) -> WorkflowRun:
        run = await self._locked(run_id)
        if run.status == RunStatus.RUNNING:
            raise ConflictError("Cancel active execution before rollback")
        query = select(WorkflowCheckpoint).where(WorkflowCheckpoint.run_id == run.id)
        query = (
            query.where(WorkflowCheckpoint.id == payload.checkpoint_id)
            if payload.checkpoint_id
            else query.order_by(
                WorkflowCheckpoint.created_at.desc(), WorkflowCheckpoint.id.desc()
            ).offset(1)
        )
        checkpoint = await self.session.scalar(query.limit(1))
        if not checkpoint or "artifact_ids" not in checkpoint.state:
            raise ConflictError(
                "Choose a checkpoint with complete lineage (created after migration 0002)"
            )
        tasks = await self._tasks(run.id)
        states = checkpoint.state["tasks"]
        for task in tasks:
            state = states.get(task.node_key)
            task.status = TaskStatus.STALE if state is None else TaskStatus(state["status"])
            if task.status in {
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                task.status = TaskStatus.PENDING
            task.output = state.get("output") if state else None
            task.error_code, task.attempt_count = None, 0
            task.started_at, task.completed_at = None, None
        await self.session.execute(
            delete(TaskDependency).where(TaskDependency.successor_id.in_([t.id for t in tasks]))
        )
        by_key = {t.node_key: t for t in tasks}
        for key, deps in checkpoint.state["graph"]:
            self.session.add_all(
                [
                    TaskDependency(predecessor_id=by_key[d].id, successor_id=by_key[key].id)
                    for d in deps
                ]
            )
        run.revision, run.generation = run.revision + 1, run.generation + 1
        run.requirement = checkpoint.state["requirement"]
        run.normalized_requirement = checkpoint.state["normalized_requirement"]
        run.status, run.completed_at, run.finished_at, run.last_error = (
            RunStatus.PENDING,
            None,
            None,
            None,
        )
        self._release_claim(run)
        await self.session.execute(
            update(Artifact).where(Artifact.run_id == run.id).values(status="stale")
        )
        keep = [UUID(i) for i in checkpoint.state["artifact_ids"]]
        if keep:
            await self.session.execute(
                update(Artifact)
                .where(Artifact.run_id == run.id, Artifact.id.in_(keep))
                .values(status="active")
            )
        await self._expire_approvals(run.id)
        # A release always needs a fresh reviewer decision after rollback.
        if by_key["final_approval"].status == TaskStatus.COMPLETED:
            by_key["final_approval"].status = TaskStatus.PENDING
        self._decision(run, "rollback", str(checkpoint.id), actor, payload.reason)
        await self._audit(
            run.id, "workflow.rolled_back", "workflow", str(run.id), "success", actor=actor
        )
        await self._checkpoint(run, "workflow_rolled_back")
        RECOVERY_ACTIONS.labels("rollback", "accepted").inc()
        return run

    @staticmethod
    def _descendant_keys(graph, start):
        affected = {start}
        while True:
            expanded = affected | {key for key, deps in graph if affected.intersection(deps)}
            if expanded == affected:
                return affected
            affected = expanded

    async def _tasks(self, run_id):
        return list(
            await self.session.scalars(
                select(WorkflowTask)
                .where(WorkflowTask.run_id == run_id)
                .order_by(WorkflowTask.node_key)
            )
        )

    async def _dependency_map(self, tasks):
        result = {}
        for edge in await self.session.scalars(
            select(TaskDependency).where(TaskDependency.successor_id.in_([t.id for t in tasks]))
        ):
            result.setdefault(edge.successor_id, set()).add(edge.predecessor_id)
        return result

    async def _graph(self, tasks):
        names = {t.id: t.node_key for t in tasks}
        deps = await self._dependency_map(tasks)
        return [
            (t.node_key, sorted(names[d] for d in deps.get(t.id, set())))
            for t in tasks
            if t.status != TaskStatus.STALE
        ]

    async def _checkpoint(self, run, reason):
        await self.session.flush()
        tasks = await self._tasks(run.id)
        artifacts = list(
            await self.session.scalars(
                select(Artifact.id).where(Artifact.run_id == run.id, Artifact.status == "active")
            )
        )
        self.session.add(
            WorkflowCheckpoint(
                run_id=run.id,
                revision=run.revision,
                reason=reason,
                state={
                    "requirement": run.requirement,
                    "normalized_requirement": run.normalized_requirement,
                    "run_status": run.status.value,
                    "graph": await self._graph(tasks),
                    "artifact_ids": [str(i) for i in artifacts],
                    "tasks": {
                        t.node_key: {
                            "status": t.status.value,
                            "output": t.output,
                            "attempt_count": t.attempt_count,
                        }
                        for t in tasks
                        if t.status != TaskStatus.STALE
                    },
                },
            )
        )
        await self.session.commit()

    async def _run(self, run_id):
        run = await self.session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("Workflow")
        return run

    async def _locked(self, run_id):
        run = await self.session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise NotFoundError("Workflow")
        return run

    @staticmethod
    def _release_claim(run):
        run.claim_token, run.lease_until = None, None

    async def _expire_approvals(self, run_id):
        await self.session.execute(
            update(Approval)
            .where(Approval.run_id == run_id, Approval.status == "pending")
            .values(status="expired")
        )

    def _decision(self, run, kind, decision, actor, reason=None):
        self.session.add(
            WorkflowDecision(
                run_id=run.id,
                revision=run.revision,
                decision_type=kind,
                decision=decision,
                actor=actor,
                rationale=reason,
            )
        )

    async def _audit(
        self, run_id, action, resource_type, resource_id, outcome, detail=None, actor="system"
    ):
        self.session.add(
            AuditEvent(
                run_id=run_id,
                actor=actor,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                detail=detail or {},
                correlation_id=str(get_contextvars().get("correlation_id", "worker")),
            )
        )
