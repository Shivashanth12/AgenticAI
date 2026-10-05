from app.core.config import Settings
from app.orchestration.brownfield import BrownfieldAnalyzer
from app.orchestration.candidates import implementation, normalize
from app.orchestration.quality import assess_artifact_quality


def test_brownfield_analysis_reports_dependencies_and_api_surface(tmp_path):
    (tmp_path / "service.py").write_text(
        "from models import Link\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/{alias}')\n"
        "def redirect(alias): return alias\n"
    )
    (tmp_path / "models.py").write_text(
        "from sqlalchemy.orm import mapped_column\n"
        "class Link: value = mapped_column()\n"
    )
    result = BrownfieldAnalyzer(Settings(workspace_path=str(tmp_path))).analyze(
        "Improve link redirect analytics"
    )
    assert result["files_inspected"] == 2
    assert any(edge["target"] == "models" for edge in result["edges"])
    assert any("GET /{alias}" in item for item in result["inventory"]["apis"])
    assert result["data_flows_to_review"]


def test_artifact_quality_accepts_deterministic_candidate():
    normalized = normalize("Build a URL shortener with analytics")
    candidate = implementation(
        "Build a URL shortener with analytics",
        {"normalized_requirement": normalized, "scenario": "greenfield"},
    )
    result = assess_artifact_quality(
        candidate,
        "def test_redirect():\n    assert True\n",
        [{"path": "README.md", "content": "Install dependencies and run the service."}],
    )
    assert result["passed"] is True
    assert result["score"] == 100


def test_artifact_quality_rejects_missing_tests_and_api():
    result = assess_artifact_quality(
        {
            "generated_files": [
                {
                    "path": "main.py",
                    "language": "python",
                    "purpose": "demo",
                    "source_code": "value = 1",
                }
            ]
        }
    )
    codes = {item["code"] for item in result["findings"]}
    assert result["passed"] is False
    assert "API_FRAMEWORK_MISSING" in codes
    assert "TEST_ARTIFACT_MISSING" in codes


async def test_workflow_assessment_endpoint(client):
    response = await client.post(
        "/api/v1/workflows",
        json={"scenario": "greenfield", "requirement": "Build URL links with analytics"},
    )
    assert response.status_code == 201
    report = await client.get(f"/api/v1/workflows/{response.json()['id']}/assessment")
    assert report.status_code == 200
    body = report.json()
    assert body["scenario"] == "greenfield"
    assert "artifact_quality" in body
    assert body["stage_evidence"]["approval_gates"]
