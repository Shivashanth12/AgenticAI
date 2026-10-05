async def test_workflow_pauses_for_design_approval(client, drive):
    response = await client.post(
        "/api/v1/workflows",
        json={
            "scenario": "greenfield",
            "requirement": "Create a secure URL shortener with governed engineering automation.",
            "risk_level": "medium",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    await drive(response.json()["id"])
    body = (await client.get(f"/api/v1/workflows/{response.json()['id']}")).json()
    assert body["status"] == "waiting_approval"
    completed = {task["key"] for task in body["tasks"] if task["status"] == "completed"}
    assert {"intake", "normalize", "decompose", "architecture", "risk"} <= completed

    approvals = await client.get(f"/api/v1/workflows/{body['id']}/approvals")
    assert approvals.status_code == 200
    assert approvals.json()[0]["status"] == "pending"

    decision = await client.post(
        f"/api/v1/workflows/{body['id']}/approvals/{approvals.json()[0]['id']}/decision",
        json={
            "decision": "approved",
            "reviewer": "test-reviewer",
            "comment": "Architecture and controls reviewed.",
        },
        headers={"Authorization": "Bearer local-reviewer-token"},
    )
    assert decision.status_code == 200
    await drive(body["id"])
    assert (await client.get(f"/api/v1/workflows/{body['id']}")).json()[
        "status"
    ] == "waiting_approval"

    final_approvals = await client.get(f"/api/v1/workflows/{body['id']}/approvals")
    final_pending = next(item for item in final_approvals.json() if item["status"] == "pending")
    final = await client.post(
        f"/api/v1/workflows/{body['id']}/approvals/{final_pending['id']}/decision",
        json={"decision": "approved", "reviewer": "release-reviewer"},
        headers={"Authorization": "Bearer local-reviewer-token"},
    )
    assert final.status_code == 200
    await drive(body["id"])
    assert (await client.get(f"/api/v1/workflows/{body['id']}")).json()["status"] == "completed"


async def test_ambiguous_workflow_requires_clarification(client, drive):
    response = await client.post(
        "/api/v1/workflows",
        json={
            "scenario": "ambiguous",
            "requirement": "Make shortened links smarter and safer.",
            "risk_level": "high",
        },
    )
    assert response.status_code == 201
    await drive(response.json()["id"])
    workflow = (await client.get(f"/api/v1/workflows/{response.json()['id']}")).json()
    waiting = next(task for task in workflow["tasks"] if task["status"] == "waiting_approval")
    assert waiting["key"] == "clarification_approval"

    approvals = (await client.get(f"/api/v1/workflows/{workflow['id']}/approvals")).json()
    rejected = await client.post(
        f"/api/v1/workflows/{workflow['id']}/approvals/{approvals[0]['id']}/decision",
        json={"decision": "approved", "reviewer": "reviewer"},
        headers={"Authorization": "Bearer local-reviewer-token"},
    )
    assert rejected.status_code == 409

    clarified = await client.post(
        f"/api/v1/workflows/{workflow['id']}/approvals/{approvals[0]['id']}/decision",
        json={
            "decision": "approved",
            "reviewer": "reviewer",
            "comment": "Block expired links and provide daily click analytics.",
        },
        headers={"Authorization": "Bearer local-reviewer-token"},
    )
    assert clarified.status_code == 200
    await drive(workflow["id"])
    assert (await client.get(f"/api/v1/workflows/{workflow['id']}")).json()[
        "status"
    ] == "waiting_approval"


async def test_brownfield_graph_and_replanning_history(client, drive):
    created = await client.post(
        "/api/v1/workflows",
        json={
            "scenario": "brownfield",
            "requirement": "Add expiring links and preserve existing redirect behavior.",
            "risk_level": "medium",
        },
    )
    assert created.status_code == 201
    await drive(created.json()["id"])
    workflow = (await client.get(f"/api/v1/workflows/{created.json()['id']}")).json()
    task_keys = {task["key"] for task in workflow["tasks"]}
    assert "codebase_analysis" in task_keys

    graph = (await client.get(f"/api/v1/workflows/{workflow['id']}/graph")).json()
    assert {
        "source": "codebase_analysis",
        "target": "decompose",
    } in graph["edges"]

    revised = await client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json={
            "requirement": "Add custom aliases and expiring links with rollback safety.",
            "reason": "Scope was refined during review.",
        },
    )
    assert revised.status_code == 200
    assert revised.json()["revision"] == 2
    await drive(workflow["id"])

    decisions = (await client.get(f"/api/v1/workflows/{workflow['id']}/decisions")).json()
    assert any(item["type"] == "requirement_revised" for item in decisions)
    checkpoints = await client.get(f"/api/v1/workflows/{workflow['id']}/checkpoints")
    assert checkpoints.status_code == 200
    assert len(checkpoints.json()) >= 2

    artifacts = await client.get(f"/api/v1/workflows/{workflow['id']}/artifacts")
    assert any(item["status"] == "stale" for item in artifacts.json())
    policies = await client.get(f"/api/v1/workflows/{workflow['id']}/policies")
    assert {item["policy"] for item in policies.json()} >= {
        "artifact_schema",
        "security",
        "data_minimization",
        "change_control",
    }


async def test_database_derived_reliability_metrics(client, drive):
    response = await client.get("/api/v1/workflows/summary/metrics")
    assert response.status_code == 200
    assert response.json()["total_runs"] == 0
