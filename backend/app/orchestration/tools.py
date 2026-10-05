"""Allowlisted checks over candidate artifacts, never over an unrelated host checkout."""

import ast
import re
from pathlib import PurePosixPath
from typing import Any

from app.core.config import Settings
from app.orchestration.candidates import digest
from app.orchestration.contracts import ImplementationContract

SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "hardcoded_secret": re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"
    ),
}
ALLOWED_FILES = {"main.py", "test_generated.py", "README.md"}


def scan(content: str) -> list[str]:
    return [rule for rule, pattern in SECRET_PATTERNS.items() if pattern.search(content)]


def candidate_files(output: dict) -> dict[str, str]:
    files = ImplementationContract.model_validate(output).model_dump()["generated_files"]
    result = {}
    for item in files:
        path = item.get("path", "")
        code = item.get("source_code")
        if (
            not isinstance(path, str)
            or path not in ALLOWED_FILES
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError("Generated path is outside the candidate allowlist")
        if path in result or not isinstance(code, str) or len(code.encode()) > 100_000:
            raise ValueError("Generated source must be unique, bounded text")
        if scan(code):
            raise ValueError("Generated content contains a potential secret")
        if path.endswith(".py"):
            ast.parse(code, filename=path)
        result[path] = code
    if "main.py" not in result:
        raise ValueError("A runnable main.py is required")
    return result


class EngineeringToolbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evidence_for(
        self, node: str, output: dict[str, Any] | None = None, context: dict | None = None
    ) -> dict[str, Any]:
        output, context = output or {}, context or {}
        if node == "implementation" or node.startswith("work_"):
            return {"sandbox_validation": self._validate_generated_files(output)}
        if node == "security":
            implementation = context.get("implementation", {})
            check = self._validate_generated_files(implementation)
            return {"executed_checks": {**check, "check": "candidate_security_and_paths"}}
        if node == "release":
            evidence = context.get("tests", {}).get("validation", {})
            security = context.get("security", {}).get("executed_checks", {})
            implementation = context.get("implementation", {})
            try:
                expected = digest(candidate_files(implementation))
            except (ValueError, SyntaxError):
                expected = None
            ready = (
                evidence.get("passed") is True
                and security.get("passed") is True
                and expected is not None
                and evidence.get("candidate_sha256") == expected
                and bool(context.get("documentation", {}).get("documents"))
            )
            return {
                "ready": ready,
                "release_evidence": [evidence, security],
                "candidate_sha256": expected,
            }
        return {}

    def _validate_generated_files(self, output: dict) -> dict:
        try:
            files = candidate_files(output)
            return {
                "check": "candidate_source_static_validation",
                "passed": True,
                "files_checked": list(files),
                "candidate_sha256": digest(files),
                "host_workspace_modified": False,
                "errors": [],
            }
        except (ValueError, SyntaxError, TypeError, AttributeError) as exc:
            return {
                "check": "candidate_source_static_validation",
                "passed": False,
                "host_workspace_modified": False,
                "errors": [str(exc)[:300]],
            }
