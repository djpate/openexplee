"""Cheaper name extraction (Haiku) and drafting emails only when asked or when sending."""
import asyncio

import aiosqlite
import httpx
import pytest

from app.config import settings
from app.engine import discovery, drafting, llm, sequencer
from app.engine.company_pool import held_domains


# ---------- helpers ----------

async def _campaign(db, name="C", vp="We build websites."):
    async with aiosqlite.connect(db) as c:
        cur = await c.execute(
            "INSERT INTO campaigns (name, icp_prompt, value_proposition) VALUES (?, 'icp', ?)", (name, vp))
        await c.commit()
        return cur.lastrowid


async def _lead(db, cid, domain, *, email=None, status="enriched", subject=None, body=None,
                summary="Family dental practice in Tampa.", verification="risky"):
    async with aiosqlite.connect(db) as c:
        cur = await c.execute(
            """INSERT INTO leads (campaign_id, company_name, company_domain, website_url, contact_name,
                   job_title, email, verification_status, status, personalized_subject, personalized_body,
                   company_summary)
               VALUES (?, ?, ?, ?, 'Jo Smith', 'Owner', ?, ?, ?, ?, ?, ?)""",
            (cid, domain.split(".")[0].title(), domain, f"https://{domain}",
             f"info@{domain}" if email is None else email,
             verification, status, subject, body, summary))
        await c.commit()
        return cur.lastrowid


async def _row(db, sql, *args):
    async with aiosqlite.connect(db) as c:
        c.row_factory = aiosqlite.Row
        return await (await c.execute(sql, args)).fetchone()


class FakeCopy:
    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    async def __call__(self, **kw):
        self.calls.append(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"subject": f"hi {kw['company_name']}", "body": f"Body for {kw['company_domain']}"}


# ---------- 1. Haiku for name extraction ----------

def test_extraction_model_defaults_to_haiku():
    assert settings.anthropic_extraction_model == "claude-haiku-5-5"


async def test_name_extraction_uses_extraction_model(monkeypatch):
    seen = []

    async def fake_call(prompt, api_key, model=None):
        seen.append(model)
        return "[]"

    monkeypatch.setattr(llm, "_call_anthropic", fake_call)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    await discovery._extract_people_with_llm("a.com", "Jo Smith, Owner of A Dental")
    assert seen[0] == "claude-haiku-5-5"


async def test_other_calls_keep_the_main_model(monkeypatch):
    seen = []

    async def fake_call(prompt, api_key, model=None):
        seen.append(model)
        return '{"subject": "s", "body": "b"}'

    monkeypatch.setattr(llm, "_call_anthropic", fake_call)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    await llm.call_llm_json("draft an email")
    assert seen == [None]  # None -> _call_anthropic uses settings.anthropic_model


# ---------- 2. drafting on demand ----------

async def test_draft_leads_drafts_only_undrafted_unsent_leads_with_email(db, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    todo = await _lead(db, cid, "a.com")
    done_already = await _lead(db, cid, "b.com", subject="old", body="old body")
    sent = await _lead(db, cid, "c.com", status="sent")
    no_email = await _lead(db, cid, "d.com", email="")

    res = await drafting.draft_leads([todo, done_already, sent, no_email])

    assert res == {"drafted": 1, "skipped": 3, "failed": 0}
    assert [c["company_domain"] for c in copy.calls] == ["a.com"]
    assert copy.calls[0]["company_summary"] == "Family dental practice in Tampa."
    assert copy.calls[0]["value_proposition"] == "We build websites."
    row = await _row(db, "SELECT personalized_subject, personalized_body FROM leads WHERE id=?", todo)
    assert (row["personalized_subject"], row["personalized_body"]) == ("hi A", "Body for a.com")
    old = await _row(db, "SELECT personalized_subject FROM leads WHERE id=?", done_already)
    assert old["personalized_subject"] == "old"


async def test_draft_rescrapes_when_lead_has_no_stored_summary(db, monkeypatch):
    copy = FakeCopy()
    scraped = []

    async def fake_scrape(url):
        scraped.append(url)
        return {"content_summary": "We fix teeth in Clearwater."}

    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    monkeypatch.setattr(drafting, "scrape_company_website", fake_scrape)
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com", summary=None)
    await drafting.draft_leads([lid])
    assert scraped == ["https://a.com"]
    assert copy.calls[0]["company_summary"] == "We fix teeth in Clearwater."


async def test_overlapping_draft_requests_draft_each_lead_once(db, monkeypatch):
    copy = FakeCopy(delay=0.05)
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    ids = [await _lead(db, cid, f"d{i}.com") for i in range(4)]
    await asyncio.gather(drafting.draft_leads(ids), drafting.draft_leads(ids))
    assert sorted(c["company_domain"] for c in copy.calls) == [f"d{i}.com" for i in range(4)]


async def test_draft_failure_is_counted_and_leaves_lead_undrafted(db, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(drafting, "generate_cold_email", boom)
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com")
    assert await drafting.draft_leads([lid]) == {"drafted": 0, "skipped": 0, "failed": 1}
    row = await _row(db, "SELECT personalized_body FROM leads WHERE id=?", lid)
    assert row["personalized_body"] is None


async def test_undrafted_lead_ids_for_campaign(db):
    cid = await _campaign(db)
    a = await _lead(db, cid, "a.com")
    await _lead(db, cid, "b.com", subject="s", body="b")
    await _lead(db, cid, "c.com", status="sent")
    await _lead(db, cid, "d.com", verification="invalid")
    assert await drafting.undrafted_lead_ids(cid) == [a]


# ---------- sending drafts just in time ----------

def _patch_sending(monkeypatch):
    sent = []

    async def inbox():
        return {"id": 1, "email": "me@x.com"}

    async def send(inbox, to_email, subject, body, lead_id=None):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return {"success": True}

    monkeypatch.setattr(sequencer, "get_next_available_inbox", inbox)
    monkeypatch.setattr(sequencer, "send_single_email", send)
    monkeypatch.setattr(sequencer.random, "randint", lambda a, b: 0)
    return sent


async def test_send_drafts_missing_copy_before_sending(db, monkeypatch):
    sent = _patch_sending(monkeypatch)
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    await _lead(db, cid, "a.com")
    res = await sequencer.dispatch_campaign_batch(cid, limit=5)
    assert res["dispatched"] == 1
    assert sent == [{"to": "info@a.com", "subject": "hi A", "body": "Body for a.com"}]


async def test_send_skips_a_lead_it_cannot_draft_instead_of_sending_generic_text(db, monkeypatch):
    sent = _patch_sending(monkeypatch)

    async def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(drafting, "generate_cold_email", boom)
    cid = await _campaign(db)
    await _lead(db, cid, "a.com")
    res = await sequencer.dispatch_campaign_batch(cid, limit=5)
    assert res["dispatched"] == 0 and sent == []
    assert any("a.com" in e or "info@a.com" in e for e in res["errors"])


# ---------- API: bulk draft, bulk delete, campaign draft ----------

@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_bulk_delete_removes_leads_and_keeps_company_reserved(db, client):
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com")
    keep = await _lead(db, cid, "b.com")
    async with aiosqlite.connect(db) as c:
        await c.execute(
            "INSERT INTO campaign_companies (campaign_id, domain, status, lead_id) VALUES (?, 'a.com', 'done', ?)",
            (cid, lid))
        await c.execute("UPDATE campaigns SET total_leads = 2 WHERE id = ?", (cid,))
        await c.commit()

    r = await client.post("/api/leads/bulk-delete", json={"lead_ids": [lid]})
    assert r.status_code == 200 and r.json() == {"deleted": 1}

    assert await _row(db, "SELECT id FROM leads WHERE id=?", lid) is None
    assert await _row(db, "SELECT id FROM leads WHERE id=?", keep) is not None
    pool = await _row(db, "SELECT status, skip_reason, lead_id FROM campaign_companies WHERE domain='a.com'")
    assert (pool["status"], pool["skip_reason"], pool["lead_id"]) == ("skipped", "deleted_by_user", None)
    camp = await _row(db, "SELECT total_leads FROM campaigns WHERE id=?", cid)
    assert camp["total_leads"] == 1
    other = await _campaign(db, name="Other")
    assert "a.com" in await held_domains(other)


async def test_bulk_draft_endpoint_queues_selected_leads(db, client, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    a = await _lead(db, cid, "a.com")
    b = await _lead(db, cid, "b.com")
    r = await client.post("/api/leads/bulk-draft", json={"lead_ids": [a, b]})
    assert r.status_code == 200 and r.json() == {"queued": 2}
    # ASGITransport runs background tasks before returning.
    assert sorted(c["company_domain"] for c in copy.calls) == ["a.com", "b.com"]


async def test_campaign_draft_endpoint_drafts_all_undrafted(db, client, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    await _lead(db, cid, "a.com")
    await _lead(db, cid, "b.com", subject="s", body="b")
    r = await client.post(f"/api/campaigns/{cid}/draft")
    assert r.status_code == 200 and r.json() == {"queued": 1}
    assert [c["company_domain"] for c in copy.calls] == ["a.com"]


async def test_campaign_list_reports_undrafted_and_drafting_counts(db, client):
    cid = await _campaign(db)
    await _lead(db, cid, "a.com")
    await _lead(db, cid, "b.com")
    await _lead(db, cid, "c.com", subject="s", body="b")
    camps = (await client.get("/api/campaigns")).json()
    c = [x for x in camps if x["id"] == cid][0]
    assert c["undrafted_count"] == 2 and c["drafting_count"] == 0


async def test_discovery_stores_summary_and_drafts_nothing(monkeypatch):
    from app.engine import lead_builder

    async def scrape(url):
        return {"domain": "a.com", "url": url, "content_summary": "Smiles in Tampa since 1990.",
                "found_emails": ["info@a.com"]}

    async def verify(email, timeout=2.0):
        return {"status": "risky", "details": ""}

    async def people(domain, text, job_titles=None):
        return []

    monkeypatch.setattr(lead_builder, "scrape_company_website", scrape)
    monkeypatch.setattr(lead_builder, "verify_email_smtp", verify)
    monkeypatch.setattr(lead_builder, "_extract_people_with_llm", people)
    async def no_rating(*a, **k):
        return None, ""
    monkeypatch.setattr(lead_builder, "rate_lead_fit", no_rating)
    assert not hasattr(lead_builder, "generate_cold_email")
    out = await lead_builder.enrich_company(
        {"id": 1, "campaign_id": 1, "domain": "a.com", "website_url": "https://a.com", "name": "A",
         "emails": "[]", "phones": "[]"}, "vp")
    lead = out["lead"]
    assert lead["company_summary"] == "Smiles in Tampa since 1990."
    assert lead["personalized_subject"] is None and lead["personalized_body"] is None


# ---------- redraft: replace existing drafts ----------

async def test_redraft_replaces_existing_drafts_but_never_sent_leads(db, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    drafted = await _lead(db, cid, "a.com", subject="old", body="old body")
    empty = await _lead(db, cid, "b.com")
    sent = await _lead(db, cid, "c.com", status="sent", subject="old", body="old body")

    res = await drafting.draft_leads([drafted, empty, sent], overwrite=True)

    assert res == {"drafted": 2, "skipped": 1, "failed": 0}
    assert sorted(c["company_domain"] for c in copy.calls) == ["a.com", "b.com"]
    row = await _row(db, "SELECT personalized_subject, personalized_body FROM leads WHERE id=?", drafted)
    assert (row["personalized_subject"], row["personalized_body"]) == ("hi A", "Body for a.com")
    kept = await _row(db, "SELECT personalized_body FROM leads WHERE id=?", sent)
    assert kept["personalized_body"] == "old body"


async def test_bulk_redraft_endpoint(db, client, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    a = await _lead(db, cid, "a.com", subject="old", body="old body")
    r = await client.post("/api/leads/bulk-draft", json={"lead_ids": [a], "redraft": True})
    assert r.status_code == 200 and r.json() == {"queued": 1}
    assert [c["company_domain"] for c in copy.calls] == ["a.com"]


async def test_campaign_redraft_endpoint_covers_drafted_and_undrafted(db, client, monkeypatch):
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    await _lead(db, cid, "a.com", subject="old", body="old body")
    await _lead(db, cid, "b.com")
    await _lead(db, cid, "c.com", status="sent", subject="s", body="b")
    r = await client.post(f"/api/campaigns/{cid}/draft?redraft=true")
    assert r.status_code == 200 and r.json() == {"queued": 2}
    assert sorted(c["company_domain"] for c in copy.calls) == ["a.com", "b.com"]


async def test_campaign_list_reports_drafted_count(db, client):
    cid = await _campaign(db)
    await _lead(db, cid, "a.com", subject="s", body="b")
    await _lead(db, cid, "b.com")
    await _lead(db, cid, "c.com", status="sent", subject="s", body="b")
    c = [x for x in (await client.get("/api/campaigns")).json() if x["id"] == cid][0]
    assert c["drafted_count"] == 1 and c["undrafted_count"] == 1


# ---------- site facts reach the draft ----------

async def test_drafting_passes_stored_site_facts(db, monkeypatch):
    import json
    copy = FakeCopy()
    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com")
    async with aiosqlite.connect(db) as c:
        await c.execute("UPDATE leads SET site_facts = ? WHERE id = ?",
                        (json.dumps({"checked": True, "lines": ["They have a contact form."]}), lid))
        await c.commit()
    await drafting.draft_leads([lid])
    assert copy.calls[0]["site_facts"] == ["They have a contact form."]


async def test_drafting_checks_the_site_when_a_lead_has_no_facts(db, monkeypatch):
    copy = FakeCopy()

    async def fake_scrape(url):
        return {"content_summary": "We fix teeth.", "final_url": "https://a.com/",
                "pages": [{"url": "https://a.com/", "html": '<a href="https://www.zocdoc.com/x">Book</a>', "kind": "home"}]}

    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    monkeypatch.setattr(drafting, "scrape_company_website", fake_scrape)
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com", summary=None)
    await drafting.draft_leads([lid])
    assert any("Zocdoc" in line for line in copy.calls[0]["site_facts"])
    row = await _row(db, "SELECT site_facts FROM leads WHERE id=?", lid)
    assert "Zocdoc" in row["site_facts"]
