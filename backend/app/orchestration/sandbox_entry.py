"""Trusted entrypoint of a disposable, network-disabled candidate validation container."""

import base64
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

from app.orchestration.tools import ALLOWED_FILES, scan


def main() -> None:
    payload = json.loads(zlib.decompress(base64.b64decode(os.environ.pop("CANDIDATE_PAYLOAD"))))
    root = Path("/candidate")
    os.chdir(root)
    for name, content in payload["files"].items():
        if name not in ALLOWED_FILES or scan(content):
            raise ValueError("Candidate violates path or secret policy")
        (root / name).write_text(content)
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": "/candidate",
        "ACCEPTANCE_FEATURES": ",".join(payload["features"]),
    }
    results = []
    commands = [
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "E9,F63,F7,F82",
            "main.py",
            "test_generated.py",
        ],
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_generated.py",
        ],
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "/opt/acceptance/test_acceptance.py"],
        [
            sys.executable,
            "-c",
            "import json; from main import app; print(json.dumps(app.openapi()))",
        ],
    ]
    for command in commands:
        # File-backed output is bounded by the container's 32 MiB tmpfs and memory limits.
        with (root / "command.log").open("w+") as log:
            try:
                result = subprocess.run(
                    command, env=environment, stdout=log, stderr=log, timeout=35, check=False
                )
                log.seek(0)
                output = log.read(24_000)
                results.append(
                    {
                        "command": command[1:],
                        "exit_code": result.returncode,
                        "passed": result.returncode == 0,
                        "output": output,
                    }
                )
            except subprocess.TimeoutExpired:
                results.append({"command": command[1:], "passed": False, "error": "timeout"})
        if not results[-1]["passed"]:
            break
    print(
        json.dumps(
            {"passed": len(results) == 4 and all(r["passed"] for r in results), "checks": results}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
