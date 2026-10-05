async def test_create_list_and_redirect(client):
    created = await client.post(
        "/api/v1/links",
        json={
            "target_url": "https://example.com/docs",
            "custom_alias": "docs-test",
            "title": "Documentation",
        },
    )
    assert created.status_code == 201
    assert created.json()["alias"] == "docs-test"

    listing = await client.get("/api/v1/links")
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    activity = await client.get("/api/v1/activity")
    assert activity.status_code == 200
    assert activity.json()[0]["action_label"] == "Link created"
    assert activity.json()[0]["resource_label"] == "Documentation"
    assert activity.json()[0]["alias"] == "docs-test"

    redirect = await client.get("/docs-test", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.com/docs"


async def test_rejects_duplicate_alias(client):
    payload = {"target_url": "https://example.com", "custom_alias": "duplicate"}
    assert (await client.post("/api/v1/links", json=payload)).status_code == 201
    response = await client.post("/api/v1/links", json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


async def test_disable_and_reenable_link(client):
    created = await client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/status", "custom_alias": "status-test"},
    )
    link = created.json()

    disabled = await client.patch(
        f"/api/v1/links/{link['id']}",
        json={"status": "disabled", "version": link["version"]},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    unavailable = await client.get("/status-test", follow_redirects=False)
    assert unavailable.status_code == 404

    enabled = await client.patch(
        f"/api/v1/links/{link['id']}",
        json={"status": "active", "version": disabled.json()["version"]},
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"


async def test_create_is_idempotent(client):
    payload = {
        "target_url": "https://example.com/idempotent",
        "custom_alias": "idempotent-link",
    }
    headers = {"Idempotency-Key": "link-request-001"}
    first = await client.post("/api/v1/links", json=payload, headers=headers)
    second = await client.post("/api/v1/links", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    conflict = await client.post(
        "/api/v1/links",
        json={**payload, "target_url": "https://example.com/different"},
        headers=headers,
    )
    assert conflict.status_code == 409


async def test_analytics_redirect_type_conflict_and_delete(client):
    created = await client.post(
        "/api/v1/links",
        json={
            "target_url": "https://example.com/report",
            "custom_alias": "report-link",
            "redirect_type": 307,
        },
    )
    link = created.json()
    redirect = await client.get("/report-link", follow_redirects=False)
    assert redirect.status_code == 307
    analytics = await client.get(f"/api/v1/links/{link['id']}/analytics")
    assert analytics.json()["total_clicks"] == 1
    assert analytics.json()["daily"][0]["clicks"] == 1

    conflict = await client.patch(
        f"/api/v1/links/{link['id']}",
        json={"title": "stale", "version": link["version"] + 1},
    )
    assert conflict.status_code == 409
    deleted = await client.delete(f"/api/v1/links/{link['id']}")
    assert deleted.status_code == 204
    assert (await client.get("/report-link", follow_redirects=False)).status_code == 404
