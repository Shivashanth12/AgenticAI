"""Trusted worker adapter: Docker authority never enters candidate containers."""

import asyncio
import base64
import json
import time
import zlib
from uuid import uuid4

from app.core.config import Settings
from app.orchestration.candidates import digest
from app.orchestration.tools import candidate_files, scan


class DockerSandbox:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def validate(
        self, implementation: dict, tests: dict, features: list[str], run_id: str
    ) -> dict:
        import docker
        from docker.types import LogConfig, Ulimit

        source_files = candidate_files(implementation)
        files = {**source_files, "test_generated.py": tests.get("test_code", "")}
        if not files["test_generated.py"].strip() or scan(files["test_generated.py"]):
            raise ValueError("Missing or unsafe generated tests")
        payload = base64.b64encode(
            zlib.compress(json.dumps({"files": files, "features": features}).encode())
        ).decode()
        if len(payload) > 60_000:
            raise ValueError("Candidate exceeds sandbox payload limit")
        client = docker.from_env(timeout=10)
        container = None
        started = time.monotonic()
        try:
            container = await asyncio.to_thread(
                client.containers.create,
                self.settings.sandbox_image,
                command=["python", "-m", "app.orchestration.sandbox_entry"],
                name=f"agentic-validation-{uuid4().hex}",
                environment={"CANDIDATE_PAYLOAD": payload},
                network_mode="none",
                read_only=True,
                user="65534:65534",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="256m",
                nano_cpus=1_000_000_000,
                pids_limit=64,
                tmpfs={
                    "/candidate": "rw,noexec,nosuid,size=32m,mode=1777",
                    "/tmp": "rw,noexec,nosuid,size=16m,mode=1777",
                },
                working_dir="/candidate",
                labels={"agentic.run": run_id},
                ulimits=[Ulimit(name="fsize", soft=1_000_000, hard=1_000_000)],
                log_config=LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
            )
            await asyncio.to_thread(container.start)
            while True:
                await asyncio.to_thread(container.reload)
                if container.status == "exited":
                    break
                if time.monotonic() - started > self.settings.sandbox_timeout_seconds:
                    raise TimeoutError("Candidate validation exceeded its time budget")
                await asyncio.sleep(0.2)
            logs = await asyncio.to_thread(container.logs, tail=1)
            result = json.loads(logs[-80_000:])
            return {
                **result,
                "candidate_sha256": digest(source_files),
                "test_sha256": digest(files["test_generated.py"]),
                "features": features,
                "isolation": "docker:no-network:nonroot:read-only",
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            if container is not None:
                await asyncio.to_thread(container.remove, force=True)
            client.close()
