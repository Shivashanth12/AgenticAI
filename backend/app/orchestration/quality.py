"""Additive quality gates for generated engineering artifacts."""

import ast
from typing import Any

from app.orchestration.tools import candidate_files, scan


def assess_artifact_quality(
    implementation: dict[str, Any], test_code: str = "", documents=None
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    documents = documents or []
    try:
        files = candidate_files(implementation)
    except (AttributeError, TypeError, ValueError, SyntaxError) as exc:
        return {
            "passed": False,
            "score": 0,
            "findings": [{"severity": "error", "code": "CANDIDATE_INVALID", "message": str(exc)}],
            "files_checked": [],
        }

    main = files.get("main.py", "")
    tree = ast.parse(main, filename="main.py")
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imports = {
        node.names[0].name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and node.names
    }
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    source = main.lower()
    if "fastapi" not in imports and "fastapi" not in imported_modules:
        findings.append(
            _finding(
                "error", "API_FRAMEWORK_MISSING", "main.py does not expose a FastAPI implementation"
            )
        )
    if "create" not in names and "post" not in source:
        findings.append(
            _finding(
                "error", "CREATE_API_MISSING", "The candidate has no visible create-link operation"
            )
        )
    if "redirect" not in source and "redirectresponse" not in source:
        findings.append(
            _finding(
                "error",
                "REDIRECT_BEHAVIOR_MISSING",
                "The candidate has no visible redirect behavior",
            )
        )
    if "sqlite" not in source and "database" not in source and "db" not in source:
        findings.append(
            _finding(
                "warning",
                "PERSISTENCE_UNCLEAR",
                "Persistence is not recognizable from the candidate source",
            )
        )
    if "raise" not in source and "http_exception" not in source:
        findings.append(
            _finding(
                "warning",
                "ERROR_HANDLING_UNCLEAR",
                "The candidate has no recognizable error handling",
            )
        )
    if scan(main):
        findings.append(
            _finding("error", "SECRET_DETECTED", "Candidate source contains a potential secret")
        )

    if not test_code.strip():
        findings.append(
            _finding("error", "TEST_ARTIFACT_MISSING", "No generated test code was supplied")
        )
    else:
        try:
            test_tree = ast.parse(test_code, filename="test_generated.py")
            test_functions = [
                node
                for node in ast.walk(test_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ]
            if not test_functions:
                findings.append(
                    _finding(
                        "error",
                        "EXECUTABLE_TEST_MISSING",
                        "Generated test code has no test function",
                    )
                )
            if not any(isinstance(node, ast.Assert) for node in ast.walk(test_tree)):
                findings.append(
                    _finding(
                        "warning",
                        "ASSERTION_MISSING",
                        "Generated tests contain no direct assertions",
                    )
                )
        except SyntaxError as exc:
            findings.append(_finding("error", "TEST_SYNTAX_INVALID", str(exc)))
        if scan(test_code):
            findings.append(
                _finding(
                    "error",
                    "TEST_SECRET_DETECTED",
                    "Generated tests contain a potential secret",
                )
            )

    document_paths = {item.get("path") for item in documents if isinstance(item, dict)}
    if "README.md" not in document_paths and "README.md" not in files:
        findings.append(
            _finding("warning", "DOCUMENTATION_MISSING", "No README.md artifact was supplied")
        )
    document_text = "\n".join(
        str(item.get("content", "")) for item in documents if isinstance(item, dict)
    )
    if document_text and not any(
        term in document_text.lower() for term in ("run", "install", "setup")
    ):
        findings.append(
            _finding(
                "warning",
                "SETUP_INSTRUCTIONS_MISSING",
                "Documentation has no recognizable setup or run instructions",
            )
        )

    deductions = sum(30 if item["severity"] == "error" else 10 for item in findings)
    return {
        "passed": not any(item["severity"] == "error" for item in findings),
        "score": max(0, 100 - deductions),
        "findings": findings,
        "files_checked": sorted(files),
        "checks": {
            "syntax": True,
            "api_surface": not any(
                item["code"].endswith("MISSING")
                for item in findings
                if item["severity"] == "error"
            ),
            "tests": not any(
                item["code"]
                in {"TEST_ARTIFACT_MISSING", "EXECUTABLE_TEST_MISSING", "TEST_SYNTAX_INVALID"}
                for item in findings
            ),
            "documentation": "README.md" in document_paths or "README.md" in files,
            "secret_scan": not any(
                item["code"] in {"SECRET_DETECTED", "TEST_SECRET_DETECTED"}
                for item in findings
            ),
        },
    }


def _finding(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}
