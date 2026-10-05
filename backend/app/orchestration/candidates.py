"""Bounded, requirement-driven URL-shortener recipes and review bundle construction."""

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.core.errors import PolicyViolationError
from app.orchestration.contracts import RequirementContract

TEMPLATES = Path(__file__).with_name("templates")
CAPABILITIES = {"aliases", "analytics", "expiration"}
CRITERIA = {
    "aliases": (
        "Create distinct HTTP(S) links; reject duplicate aliases; "
        "redirect with 301/302/307; unknown aliases return 404."
    ),
    "analytics": ("Record every successful redirect; return accurate total and daily counts."),
    "expiration": (
        "Require timezone-aware future expiration; redirect before expiry; "
        "return 410 afterward without counting a click."
    ),
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize(requirement: str) -> dict:
    words = set(re.findall(r"[a-z]+", requirement.lower()))
    if not words.intersection(
        {"url", "urls", "link", "links", "shortener", "shortened", "alias", "aliases"}
    ):
        raise PolicyViolationError(
            "Supported scope is URL-shortener engineering; clarify the requirement"
        )
    unsupported = words.intersection(
        {"billing", "payments", "geolocation", "qr", "oauth", "kubernetes", "caching", "cache"}
    )
    if unsupported:
        raise PolicyViolationError(
            f"Unsupported candidate capabilities: {', '.join(sorted(unsupported))}"
        )
    features = ["aliases"]
    if words.intersection({"analytics", "click", "clicks", "daily", "statistics"}):
        features.append("analytics")
    if words.intersection({"expiration", "expiry", "expire", "expires", "expiring", "expired"}):
        features.append("expiration")
    ambiguities = [
        f"Define measurable meaning of '{word}'."
        for word in sorted(
            words.intersection({"smart", "smarter", "better", "safe", "safer", "fast", "scalable"})
        )
    ]
    return RequirementContract.model_validate(
        {
            "problem": requirement,
            "features": features,
            "ambiguities": ambiguities,
            "acceptance_criteria": [{"id": key, "behavior": CRITERIA[key]} for key in features],
            "scope": "Runnable local URL-shortener candidate",
            "non_goals": ["deployment", "host changes"],
        }
    ).model_dump()


def source(features: list[str]) -> str:
    code = (
        (TEMPLATES / "shortener.py.txt")
        .read_text()
        .replace("__ANALYTICS__", str("analytics" in features))
    )
    if "expiration" not in features:
        code = code.replace("    expires_at: datetime | None = None\n", "")
        start = code.index('    @field_validator("expires_at")')
        end = code.index("\n\n@app.post", start)
        code = code[:start] + code[end:]
        start = code.index("    # Backward-compatible additive upgrade")
        end = code.index('    db.execute("CREATE TABLE IF NOT EXISTS clicks', start)
        code = code[:start] + code[end:]
        code = code.replace(
            "url TEXT NOT NULL, expires REAL, redirect_type", "url TEXT NOT NULL, redirect_type"
        )
        code = code.replace(
            "    expires = payload.expires_at.timestamp() if payload.expires_at else None\n", ""
        )
        code = code.replace(
            "(alias, url, expires, redirect_type) VALUES (?, ?, ?, ?)",
            "(alias, url, redirect_type) VALUES (?, ?, ?)",
        )
        code = code.replace(
            "str(payload.target_url), expires, payload.redirect_type",
            "str(payload.target_url), payload.redirect_type",
        )
        code = code.replace(
            '            if row["expires"] is not None and row["expires"] <= clock():\n'
            '                raise HTTPException(410, "Expired")\n',
            "",
        )
    return code


def baseline(scenario: str) -> dict[str, str]:
    # Frozen greenfield recipe output; brownfield analysis and patching use this exact source.
    return {"main.py": source(["aliases", "analytics"])} if scenario == "brownfield" else {}


def implementation(requirement: str, context: dict) -> dict:
    normalized = context["normalized_requirement"]
    features = normalized["features"]
    files = {"main.py": source(features)}
    original = baseline(context.get("scenario", "greenfield"))
    patch = "".join(
        line
        for path, code in files.items()
        for line in difflib.unified_diff(
            original.get(path, "").splitlines(True),
            code.splitlines(True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    for_path = [
        {"path": path, "language": "python", "purpose": requirement, "source_code": code}
        for path, code in files.items()
    ]
    return {
        "generated_files": for_path,
        "features": features,
        "baseline_sha256": digest(original),
        "baseline_files": original,
        "patch": patch,
        "candidate_sha256": digest(files),
        "provenance": "bounded deterministic recipe",
        "acceptance_criteria": normalized["acceptance_criteria"],
    }


def generated_test() -> str:
    return """from fastapi.testclient import TestClient
import main

def test_generated_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", str(tmp_path / "generated.db"))
    client = TestClient(main.app)
    result = client.post("/api/links", json={
        "target_url": "https://example.com", "custom_alias": "generated"})
    assert result.status_code == 201
    response = client.get("/generated", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"
"""


def documentation(features: list[str]) -> str:
    return (
        "# Generated URL shortener\n\n"
        "Requires Python 3.12. Install `fastapi uvicorn httpx pytest`.\n"
        "Run `uvicorn main:app --port 8080`; open `/docs` for the API contract.\n"
        "Run `pytest -q test_generated.py`. "
        "The bundle contains independent validation evidence.\n\n"
        f"Implemented capabilities: {', '.join(features)}.\n\n"
        "SQLite stores links and clicks in links.db. Back up that file while stopped.\n"
        "This is a local development artifact; "
        "protect management routes before deployment.\n"
        "No host changes or deployment were performed. "
        "Rollback restores the bundled baseline.\n"
    )
