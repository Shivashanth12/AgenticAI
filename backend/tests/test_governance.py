import asyncio
import time

import pytest
from app.core.config import Settings
from app.core.errors import ProviderError
from app.orchestration.candidates import normalize
from app.orchestration.providers import (
    AIProvider,
    DeterministicProvider,
    FallbackProvider,
    validate_stage_output,
)
from app.orchestration.tools import EngineeringToolbox


class AlwaysFails(AIProvider):
    name = "failure-injection"

    async def execute(self, node, requirement, context):
        raise RuntimeError("injected provider failure")


class TimedProvider(DeterministicProvider):
    name = "timed"

    def __init__(self):
        self.intervals = {}

    async def execute(self, node, requirement, context):
        started = time.perf_counter()
        if node in {"architecture", "risk"}:
            await asyncio.sleep(0.05)
        output = await super().execute(node, requirement, context)
        self.intervals[node] = (started, time.perf_counter())
        return output


class ProviderUnavailable(AIProvider):
    name = "unavailable"

    async def execute(self, node, requirement, context):
        raise ProviderError("offline")


async def test_provider_falls_back_without_paid_api():
    provider = FallbackProvider(ProviderUnavailable(), DeterministicProvider())
    output = await provider.execute("intake", "Build a secure shortener", {})
    assert output["fallback"]["used"] is True
    assert output["fallback"]["to"] == "deterministic"


async def test_stage_descriptions_are_specific():
    provider = DeterministicProvider()
    requirement = "Build expiring links with daily analytics"
    outputs = [
        await provider.execute(
            node, requirement, {"normalized_requirement": normalize(requirement)}
        )
        for node in ("intake", "normalize", "decompose", "risk")
    ]
    for output in outputs:
        assert requirement in output["summary"]
        assert "generated artifact that meets" not in str(output).lower()


def test_work_items_use_the_implementation_contract():
    with pytest.raises(ProviderError):
        validate_stage_output("work_aliases", {"summary": "missing generated files"})


async def test_parallel_branches_overlap(client, monkeypatch, drive):
    provider = TimedProvider()
    monkeypatch.setattr(
        "app.orchestration.engine.build_provider",
        lambda settings: provider,
    )
    response = await client.post(
        "/api/v1/workflows",
        json={
            "scenario": "greenfield",
            "requirement": "Create a secure URL shortener with analytics.",
            "risk_level": "medium",
        },
    )
    assert response.status_code == 201
    await drive(response.json()["id"])
    architecture = provider.intervals["architecture"]
    risk = provider.intervals["risk"]
    assert architecture[0] < risk[1] and risk[0] < architecture[1]


async def test_exhausted_retries_safe_stop(client, monkeypatch, drive):
    monkeypatch.setattr(
        "app.orchestration.engine.build_provider",
        lambda settings: AlwaysFails(),
    )

    async def no_delay(_):
        return None

    monkeypatch.setattr("app.orchestration.engine.asyncio.sleep", no_delay)
    response = await client.post(
        "/api/v1/workflows",
        json={
            "scenario": "greenfield",
            "requirement": "Create a reliable URL shortener service.",
            "risk_level": "high",
        },
    )
    assert response.status_code == 201
    await drive(response.json()["id"])
    assert (await client.get(f"/api/v1/workflows/{response.json()['id']}")).json()[
        "status"
    ] == "safe_stopped"
    attempts = await client.get(f"/api/v1/workflows/{response.json()['id']}/attempts")
    assert len(attempts.json()) == 3
    assert all(item["status"] == "failed" for item in attempts.json())


async def test_destination_security_rejects_private_network(client):
    response = await client.post(
        "/api/v1/links",
        json={"target_url": "http://127.0.0.1/internal", "custom_alias": "private"},
    )
    assert response.status_code == 422


async def test_role_boundaries_and_identity(client):
    unauthenticated = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer invalid"}
    )
    assert unauthenticated.status_code == 401
    reviewer_cannot_create = await client.post(
        "/api/v1/workflows",
        json={"scenario": "greenfield", "requirement": "Build a governed shortener"},
        headers={"Authorization": "Bearer local-reviewer-token"},
    )
    assert reviewer_cannot_create.status_code == 403
    identity = await client.get("/api/v1/auth/me")
    assert identity.json() == {"actor": "local-requester", "role": "requester"}


def test_generated_source_sandbox_rejects_path_traversal(tmp_path):
    toolbox = EngineeringToolbox(Settings(workspace_path=str(tmp_path)))
    evidence = toolbox.evidence_for(
        "implementation",
        {"generated_files": [{"path": "../escape.py", "source_code": "value = 1"}]},
    )
    assert evidence["sandbox_validation"]["passed"] is False
    assert evidence["sandbox_validation"]["host_workspace_modified"] is False
    assert not (tmp_path.parent / "escape.py").exists()
