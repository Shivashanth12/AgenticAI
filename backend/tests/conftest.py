# ruff: noqa: E402
# Configuration must be set before importing the application.
import os
import tempfile

_test_directory = tempfile.TemporaryDirectory(prefix="agentic-tests-")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_test_directory.name}/test.db"
)
os.environ["WORKSPACE_PATH"] = os.getcwd()
os.environ["LLM_PROVIDER"] = "deterministic"

import pytest
import pytest_asyncio
from app.db.base import Base, SessionFactory, engine
from app.main import app
from app.orchestration.candidates import digest
from app.orchestration.engine import WorkflowEngine
from app.orchestration.tools import candidate_files
from httpx import ASGITransport, AsyncClient


class SandboxDouble:
    """Orchestration unit-test double only. Real Docker checks live in test_sandbox.py."""

    async def validate(self, implementation, tests, features, run_id):
        return {
            "passed": True,
            "candidate_sha256": digest(candidate_files(implementation)),
            "isolation": "UNIT_TEST_DOUBLE",
            "features": features,
        }


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer local-requester-token"},
    ) as test_client:
        yield test_client
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


@pytest.fixture
def drive():
    async def execute(run_id):
        from uuid import UUID

        async with SessionFactory() as session:
            return await WorkflowEngine(session, sandbox=SandboxDouble()).execute(UUID(str(run_id)))

    return execute
