"""Email instructions are a per-campaign setting; NULL means the live default."""
import aiosqlite
import httpx
import pytest

from app.engine import copywriter, drafting


@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _instructions(db, cid):
    async with aiosqlite.connect(db) as c:
        return (await (await c.execute("SELECT draft_instructions FROM campaigns WHERE id=?", (cid,))).fetchone())[0]


async def _create(client, **extra):
    r = await client.post("/api/campaigns", json={
        "name": "C", "icp_prompt": "dentists in Tampa", "value_proposition": "vp", **extra})
    return r.json()["id"]


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    # Creating a campaign schedules discovery; these tests only care about the stored instructions.
    from app.api import campaigns

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(campaigns, "run_discovery_task", nothing)


async def test_default_instructions_endpoint(client):
    r = await client.get("/api/campaigns/draft-instructions/default")
    assert r.status_code == 200 and r.json()["instructions"] == copywriter.default_draft_instructions()


async def test_create_with_custom_instructions_stores_them(db, client):
    cid = await _create(client, draft_instructions="Keep it to 50 words.")
    assert await _instructions(db, cid) == "Keep it to 50 words."


async def test_create_with_default_or_blank_instructions_stores_null(db, client):
    a = await _create(client, draft_instructions=copywriter.default_draft_instructions())
    b = await _create(client, draft_instructions="  ")
    c = await _create(client)
    assert [await _instructions(db, x) for x in (a, b, c)] == [None, None, None]


async def test_update_and_reset_instructions(db, client):
    cid = await _create(client)
    r = await client.put(f"/api/campaigns/{cid}/draft-instructions", json={"instructions": "Mention our free audit."})
    assert r.status_code == 200 and await _instructions(db, cid) == "Mention our free audit."
    listed = [c for c in (await client.get("/api/campaigns")).json() if c["id"] == cid][0]
    assert listed["draft_instructions"] == "Mention our free audit."
    r = await client.put(f"/api/campaigns/{cid}/draft-instructions", json={"instructions": None})
    assert r.status_code == 200 and await _instructions(db, cid) is None
    assert (await client.put("/api/campaigns/9999/draft-instructions", json={"instructions": "x"})).status_code == 404


async def test_drafting_uses_the_campaign_instructions(db, monkeypatch):
    calls = []

    async def copy(**kw):
        calls.append(kw)
        return {"subject": "s", "body": "b"}

    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    async with aiosqlite.connect(db) as c:
        custom = (await c.execute("INSERT INTO campaigns (name, icp_prompt, value_proposition, draft_instructions) "
                                  "VALUES ('A', 'i', 'vp', 'Be brief.')")).lastrowid
        plain = (await c.execute("INSERT INTO campaigns (name, icp_prompt, value_proposition) VALUES ('B', 'i', 'vp')")).lastrowid
        ids = []
        for cid, dom in ((custom, "a.com"), (plain, "b.com")):
            ids.append((await c.execute(
                "INSERT INTO leads (campaign_id, company_name, company_domain, email, verification_status, status, company_summary) "
                "VALUES (?, 'X', ?, ?, 'risky', 'enriched', 's')", (cid, dom, f"info@{dom}"))).lastrowid)
        await c.commit()
    await drafting.draft_leads(ids)
    by_domain = {k["company_domain"]: k.get("instructions") for k in calls}
    assert by_domain == {"a.com": "Be brief.", "b.com": None}
