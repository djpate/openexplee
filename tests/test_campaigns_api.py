import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import campaigns
from app.database import get_db_connection
from app.engine.overture import OvertureError
from app.engine.search_plan import PlanError
from app.main import app


@pytest.fixture
def calls(monkeypatch):
    c = {"refresh": [], "batch": []}

    async def fake_refresh(cid):
        c["refresh"].append(cid)
        return {"inserted": 0, "pool_total": 0, "messages": []}

    async def fake_batch(cid, size, vp, target):
        c["batch"].append((cid, size, vp, target))
        return {"processed": 3, "leads": 2, "skipped": 1, "lead_ids": [1, 2]}

    monkeypatch.setattr(campaigns, "refresh_campaign_pool", fake_refresh)
    monkeypatch.setattr(campaigns, "process_next_batch", fake_batch)
    return c


async def _row(cid):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,))
        return dict(await cur.fetchone())
    finally:
        await conn.close()


async def _make(status="draft", plan=None, batch_size=50, release=None):
    """release set = a search has completed for this plan (pool built from that release)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "INSERT INTO campaigns (name, icp_prompt, value_proposition, status, search_plan, batch_size, "
            "overture_release) VALUES ('c', 'dentists', 'vp', ?, ?, ?, ?)", (status, plan, batch_size, release))
        await conn.commit()
        return cur.lastrowid
    finally:
        await conn.close()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_create_runs_search_then_one_batch(db, calls):
    cid = await _make()
    await campaigns.run_discovery_task(cid)
    assert calls["refresh"] == [cid] and calls["batch"] == [(cid, 50, "vp", "dentists")]
    assert (await _row(cid))["status"] == "ready"


async def test_discover_does_not_search_again_when_plan_exists(db, calls):
    cid = await _make(plan='{"x": 1}', release="2026-10-21.0")
    await campaigns.run_discovery_task(cid)
    assert calls["refresh"] == [] and len(calls["batch"]) == 1


async def test_discover_resumes_search_when_plan_stored_but_fetch_never_completed(db, calls):
    # The plan is stored before the fetch; a failed or interrupted first fetch leaves no release.
    cid = await _make(status="error", plan='{"x": 1}')
    await campaigns.run_discovery_task(cid)
    assert calls["refresh"] == [cid] and len(calls["batch"]) == 1


async def test_refresh_endpoint_searches_but_creates_no_leads(db, calls):
    cid = await _make()
    async with await _client() as c:
        r = await c.post(f"/api/campaigns/{cid}/refresh-search")
        assert r.status_code == 200 and r.json() == {"id": cid, "status": "discovering"}
        assert (await c.post("/api/campaigns/9999/refresh-search")).status_code == 404
    assert calls["refresh"] == [cid] and calls["batch"] == []
    assert (await _row(cid))["status"] == "ready"


async def test_overture_failure_sets_error_and_message(db, calls, monkeypatch):
    async def boom(cid):
        raise OvertureError("HTTP 503 from s3")
    monkeypatch.setattr(campaigns, "refresh_campaign_pool", boom)
    cid = await _make()
    await campaigns.run_discovery_task(cid)
    row = await _row(cid)
    assert row["status"] == "error" and "HTTP 503" in row["discovery_message"]
    assert calls["batch"] == []


async def test_plan_error_message_shown(db, calls, monkeypatch):
    async def boom(cid):
        raise PlanError("Could not understand the target.")
    monkeypatch.setattr(campaigns, "refresh_campaign_pool", boom)
    cid = await _make()
    await campaigns.run_refresh_task(cid)
    row = await _row(cid)
    assert row["status"] == "error" and row["discovery_message"] == "Could not understand the target."


async def test_unexpected_failure_message(db, calls, monkeypatch):
    async def boom(cid, size, vp, target):
        raise KeyError("x")
    monkeypatch.setattr(campaigns, "process_next_batch", boom)
    cid = await _make(plan="{}")
    await campaigns.run_discovery_task(cid)
    row = await _row(cid)
    assert row["status"] == "error" and row["discovery_message"].startswith("Discovery failed: KeyError:")


async def test_list_returns_parsed_plan_and_pool_counts(db, calls):
    cid = await _make(plan=json.dumps({"categories": ["dental_clinic"]}))
    conn = await get_db_connection()
    try:
        for i, st in enumerate(["done", "done", "skipped", "processing", "pending"]):
            await conn.execute(
                "INSERT INTO campaign_companies (campaign_id, domain, status) VALUES (?, ?, ?)", (cid, f"d{i}.com", st))
        await conn.commit()
    finally:
        await conn.close()
    other = await _make()
    async with await _client() as c:
        items = {i["id"]: i for i in (await c.get("/api/campaigns")).json()}
    it = items[cid]
    assert it["search_plan"] == {"categories": ["dental_clinic"]}
    assert (it["pool_done"], it["pool_skipped"], it["pool_processing"]) == (2, 1, 1)
    assert "pool_total" in it and "pool_pending" in it and "discovery_message" in it
    assert items[other]["search_plan"] is None and items[other]["pool_done"] == 0


async def test_autopilot_sets_batch_size_from_leads_per_campaign(db, calls, monkeypatch):
    async def fake_analyze(url):
        return {"value_proposition": "vp", "product_name": "P",
                "target_icps": [{"campaign_name": "A", "icp_query": "dentists"}]}
    monkeypatch.setattr(campaigns, "analyze_my_product", fake_analyze)
    async with await _client() as c:
        r = await c.post("/api/campaigns/autopilot", json={"url": "x.com", "leads_per_campaign": 200})
    assert r.status_code == 200
    cid = r.json()["campaigns_created"][0]["id"]
    assert (await _row(cid))["batch_size"] == 200


async def test_batch_size_default_and_clamp(db, calls):
    async with await _client() as c:
        ids = []
        for bs in (None, 5000):
            body = {"name": "n", "icp_prompt": "p", "value_proposition": "v"}
            if bs:
                body["batch_size"] = bs
            ids.append((await c.post("/api/campaigns", json=body)).json()["id"])
    assert [(await _row(i))["batch_size"] for i in ids] == [50, 1000]


@pytest.mark.parametrize("path", ["discover", "refresh-search"])
async def test_409_when_already_discovering(db, calls, path):
    cid = await _make(status="discovering")
    async with await _client() as c:
        r = await c.post(f"/api/campaigns/{cid}/{path}")
    assert r.status_code == 409
    assert r.json() == {"detail": "Discovery is already running for this campaign."}


@pytest.mark.parametrize("path", ["discover", "refresh-search"])
async def test_concurrent_requests_only_one_wins(db, calls, path, monkeypatch):
    import asyncio

    async def slow_refresh(cid):
        await asyncio.sleep(0.3)
        return {"inserted": 0, "pool_total": 0, "messages": []}

    async def slow_batch(cid, size, vp, target):
        await asyncio.sleep(0.3)
        return {"processed": 0, "leads": 0, "skipped": 0, "lead_ids": []}

    monkeypatch.setattr(campaigns, "refresh_campaign_pool", slow_refresh)
    monkeypatch.setattr(campaigns, "process_next_batch", slow_batch)
    cid = await _make()
    async with await _client() as c:
        rs = await asyncio.gather(*[c.post(f"/api/campaigns/{cid}/{path}") for _ in range(2)])
    assert sorted(r.status_code for r in rs) == [200, 409]


async def _set_message(cid, msg):
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE campaigns SET discovery_message = ? WHERE id = ?", (msg, cid))
        await conn.commit()
    finally:
        await conn.close()


async def test_successful_discover_with_plan_clears_stale_message(db, calls):
    cid = await _make(status="error", plan='{"x": 1}', release="2026-10-21.0")
    await _set_message(cid, "HTTP 503 from s3")
    await campaigns.run_discovery_task(cid)
    row = await _row(cid)
    assert row["status"] == "ready" and row["discovery_message"] is None and calls["refresh"] == []


async def test_successful_first_discover_shows_new_search_messages(db, calls, monkeypatch):
    async def refresh(cid):
        return {"inserted": 1, "pool_total": 1, "messages": ["note a", "note b"]}
    monkeypatch.setattr(campaigns, "refresh_campaign_pool", refresh)
    cid = await _make(status="error")
    await _set_message(cid, "Interrupted by a restart. Click Find next leads to resume.")
    await campaigns.run_discovery_task(cid)
    assert (await _row(cid))["discovery_message"] == "note a\nnote b"


@pytest.mark.parametrize("new,expected", [(["fresh note"], "fresh note"), ([], None)])
async def test_successful_refresh_replaces_stale_message(db, calls, monkeypatch, new, expected):
    async def refresh(cid):
        return {"inserted": 0, "pool_total": 0, "messages": new}
    monkeypatch.setattr(campaigns, "refresh_campaign_pool", refresh)
    cid = await _make(status="error", plan='{"x": 1}')
    await _set_message(cid, "HTTP 503 from s3")
    await campaigns.run_refresh_task(cid)
    row = await _row(cid)
    assert row["status"] == "ready" and row["discovery_message"] == expected


@pytest.mark.parametrize("path", ["discover", "refresh-search"])
async def test_409_while_sending(db, calls, path):
    cid = await _make(status="sending")
    async with await _client() as c:
        r = await c.post(f"/api/campaigns/{cid}/{path}")
    assert r.status_code == 409
    assert r.json() == {"detail": "Campaign is sending; try again when it finishes."}
    assert (await _row(cid))["status"] == "sending" and calls["refresh"] == calls["batch"] == []
