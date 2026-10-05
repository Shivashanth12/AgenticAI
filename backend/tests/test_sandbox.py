"""Real Docker isolation and generated-code tests; opt in with RUN_DOCKER_TESTS=1."""

import asyncio
import os

import pytest
from app.core.config import Settings
from app.orchestration.candidates import generated_test, implementation, normalize
from app.orchestration.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_TESTS") != "1", reason="Requires built sandbox image and Docker"
)


def candidate(requirement):
    return implementation(
        requirement, {"normalized_requirement": normalize(requirement), "scenario": "greenfield"}
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "Build URL aliases with daily analytics",
        "Build URL aliases with expiration and daily analytics",
    ],
)
async def test_real_candidate_acceptance(requirement):
    output = candidate(requirement)
    result = await DockerSandbox(Settings()).validate(
        output, {"test_code": generated_test()}, output["features"], "integration"
    )
    assert result["passed"], result
    assert result["isolation"] == "docker:no-network:nonroot:read-only"
    assert len(result["checks"]) == 4


async def test_failing_generated_assertion_blocks_release():
    output = candidate("Build URL aliases")
    result = await DockerSandbox(Settings()).validate(
        output,
        {"test_code": "def test_failure():\n    assert False\n"},
        output["features"],
        "failing-test",
    )
    assert result["passed"] is False
    assert any(check.get("exit_code") == 1 for check in result["checks"])


async def test_broken_generated_implementation_is_rejected():
    output = candidate("Build URL aliases")
    output["generated_files"][0]["source_code"] = output["generated_files"][0][
        "source_code"
    ].replace('status_code=row["redirect_type"]', "status_code=200")
    result = await DockerSandbox(Settings()).validate(
        output, {"test_code": generated_test()}, output["features"], "broken-implementation"
    )
    assert result["passed"] is False


async def test_timeout_removes_container():
    import docker

    output = candidate("Build URL aliases")
    validator = DockerSandbox(Settings(sandbox_timeout_seconds=1))
    with pytest.raises(TimeoutError):
        await validator.validate(
            output,
            {"test_code": "def test_hang():\n    while True: pass\n"},
            output["features"],
            "timeout-test",
        )
    client = docker.from_env()
    assert not client.containers.list(all=True, filters={"label": "agentic.run=timeout-test"})
    client.close()


async def test_cancel_removes_active_validation():
    import docker

    output = candidate("Build URL aliases")
    task = asyncio.create_task(
        DockerSandbox(Settings()).validate(
            output,
            {"test_code": "def test_hang():\n    while True: pass\n"},
            output["features"],
            "cancel-test",
        )
    )
    await asyncio.sleep(2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client = docker.from_env()
    assert not client.containers.list(all=True, filters={"label": "agentic.run=cancel-test"})
    client.close()
