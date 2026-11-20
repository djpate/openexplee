"""AI fit rating (1-5 + reason) against the campaign's offer; disqualify below a per-campaign threshold."""
import aiosqlite
import httpx
import pytest

from app.config import settings
from app.engine import drafting, fit, sequencer


async def _campaign(db, min_fit=3, vp="We build websites and booking tools."):
    async with aiosqlite.connect(db) as c:
        cid = (await c.execute(
            "INSERT INTO campaigns (name, icp_prompt, value_proposition, min_fit_score) VALUES ('C', 'i', ?, ?)",
            (vp, min_fit))).lastrowid
        await c.commit()
        return cid


async def _lead(db, cid, domain, score=None, override=None, body=None, reason=None):
    async with aiosqlite.connect(db) as c:
        lid = (await c.execute(
            """INSERT INTO leads (campaign_id, company_name, company_domain, email, verification_status, status,
                   company_summary, fit_score, fit_reason, fit_override, personalized_subject, personalized_body)
               VALUES (?, ?, ?, ?, 'risky', 'enriched', 'A dance studio.', ?, ?, ?, ?, ?)""",
            (cid, domain, domain, f"info@{domain}", score, reason, override, body and "s", body))).lastrowid
        await c.commit()
        return lid


async def _col(db, sql, *args):
    async with aiosqlite.connect(db) as c:
        return (await (await c.execute(sql, args)).fetchone())[0]


# ---------- rating call ----------

@pytest.fixture
def llm(monkeypatch):
    calls = []
    reply = {"value": {"score": 2, "reason": "Already books through Mindbody."}}

    async def fake(prompt, provider=None, api_key=None, model=None):
        calls.append({"prompt": prompt, "model": model})
        return reply["value"]

    monkeypatch.setattr(fit, "call_llm_json", fake)
    return calls, reply


async def test_rating_uses_the_bulk_model_and_judges_against_the_offer(llm):
    calls, _ = llm
    score, reason = await fit.rate_lead_fit("Studio", "studio.com", "Dance classes.",
                                            ["They already use Mindbody for online booking."], "We build booking tools.")
    assert (score, reason) == (2, "Already books through Mindbody.")
    assert calls[0]["model"] == settings.anthropic_extraction_model
    p = calls[0]["prompt"]
    assert "We build booking tools." in p and "They already use Mindbody" in p
    assert "1 to 5" in p and "use 3" in p


@pytest.mark.parametrize("bad", [None, "nope", {"score": 9, "reason": "x"}, {"score": "high"}, {"reason": "x"}])
async def test_unusable_rating_returns_none(llm, bad):
    _, reply = llm
    reply["value"] = bad
    assert (await fit.rate_lead_fit("S", "s.com", "", [], "vp"))[0] is None


# ---------- who counts as qualified ----------

async def test_threshold_overrides_and_unrated(db):
    cid = await _campaign(db, min_fit=3)
    low = await _lead(db, cid, "low.com", score=2)
    ok = await _lead(db, cid, "ok.com", score=4)
    unrated = await _lead(db, cid, "new.com")
    rescued = await _lead(db, cid, "rescued.com", score=1, override="qualified")
    dropped = await _lead(db, cid, "dropped.com", score=5, override="disqualified")
    async with aiosqlite.connect(db) as c:
        rows = await (await c.execute(f"SELECT id FROM leads WHERE {fit.qualified_sql()} ORDER BY id")).fetchall()
    assert [r[0] for r in rows] == [ok, unrated, rescued]
    assert low not in [r[0] for r in rows] and dropped not in [r[0] for r in rows]


async def test_raising_the_threshold_disqualifies_more(db):
    cid = await _campaign(db, min_fit=4)
    await _lead(db, cid, "three.com", score=3)
    assert await _col(db, f"SELECT COUNT(*) FROM leads WHERE {fit.qualified_sql()}") == 0


# ---------- disqualified leads are not drafted or sent ----------

async def test_drafting_skips_disqualified_leads(db, monkeypatch):
    drafted = []

    async def copy(**kw):
        drafted.append(kw["company_domain"])
        return {"subject": "s", "body": "b"}

    monkeypatch.setattr(drafting, "generate_cold_email", copy)
    cid = await _campaign(db)
    good = await _lead(db, cid, "good.com", score=4)
    bad = await _lead(db, cid, "bad.com", score=1)
    await drafting.draft_leads([good, bad])
    assert drafted == ["good.com"]
    assert await drafting.undrafted_lead_ids(cid) == []


async def test_sending_skips_disqualified_leads(db, monkeypatch):
    sent = []

    async def inbox():
        return {"id": 1, "email": "me@x.com"}

    async def send(inbox, to_email, subject, body, lead_id=None):
        sent.append(to_email)
        return {"success": True}

    monkeypatch.setattr(sequencer, "get_next_available_inbox", inbox)
    monkeypatch.setattr(sequencer, "send_single_email", send)
    monkeypatch.setattr(sequencer.random, "randint", lambda a, b: 0)
    cid = await _campaign(db)
    await _lead(db, cid, "good.com", score=5, body="b")
    await _lead(db, cid, "bad.com", score=1, body="b")
    await sequencer.dispatch_campaign_batch(cid, limit=10)
    assert sent == ["info@good.com"]


# ---------- rating existing leads ----------

async def test_rate_leads_fills_unrated_and_rerates_when_asked(db, llm):
    calls, reply = llm
    cid = await _campaign(db)
    new = await _lead(db, cid, "new.com")
    old = await _lead(db, cid, "old.com", score=5, reason="was great")
    assert await fit.rate_leads([new, old]) == {"rated": 1, "skipped": 1, "failed": 0}
    assert await _col(db, "SELECT fit_score FROM leads WHERE id=?", new) == 2
    assert await _col(db, "SELECT fit_score FROM leads WHERE id=?", old) == 5
    reply["value"] = {"score": 4, "reason": "Needs booking."}
    assert (await fit.rate_leads([old], overwrite=True))["rated"] == 1
    assert await _col(db, "SELECT fit_reason FROM leads WHERE id=?", old) == "Needs booking."
    assert "We build websites and booking tools." in calls[0]["prompt"]


async def test_rating_describes_stored_site_facts_in_the_current_wording(db, llm):
    import json
    from tests.test_lazy_drafting import STALE_FACTS
    calls, _ = llm
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com")
    async with aiosqlite.connect(db) as c:
        await c.execute("UPDATE leads SET site_facts = ? WHERE id = ?", (json.dumps(STALE_FACTS), lid))
        await c.commit()
    assert (await fit.rate_leads([lid]))["rated"] == 1
    p = calls[0]["prompt"]
    assert "didn't find" not in p
    assert "own booking page (/book-now)" in p


# ---------- discovery rates new leads ----------

async def test_new_leads_are_rated_during_discovery(monkeypatch):
    from app.engine import lead_builder

    async def scrape(url):
        return {"domain": "a.com", "url": url, "content_summary": "Smiles in Tampa.", "found_emails": ["info@a.com"],
                "final_url": "https://a.com/", "pages": [{"url": "https://a.com/", "html": "<p>x</p>", "kind": "home"}]}

    async def verify(email, timeout=2.0):
        return {"status": "risky", "details": ""}

    async def people(domain, text, job_titles=None):
        return []

    async def rate(company_name, company_domain, summary, site_facts, value_proposition):
        assert value_proposition == "our offer" and site_facts
        return 4, "No online booking visible."

    monkeypatch.setattr(lead_builder, "scrape_company_website", scrape)
    monkeypatch.setattr(lead_builder, "verify_email_smtp", verify)
    monkeypatch.setattr(lead_builder, "_extract_people_with_llm", people)
    monkeypatch.setattr(lead_builder, "rate_lead_fit", rate)
    lead = (await lead_builder.enrich_company(
        {"id": 1, "campaign_id": 1, "domain": "a.com", "website_url": "https://a.com", "name": "A",
         "emails": "[]", "phones": "[]"}, "our offer"))["lead"]
    assert (lead["fit_score"], lead["fit_reason"]) == (4, "No online booking visible.")


# ---------- API ----------

@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_override_and_threshold_endpoints(db, client):
    cid = await _campaign(db)
    lid = await _lead(db, cid, "a.com", score=1)
    r = await client.put(f"/api/leads/{lid}/qualification", json={"override": "qualified"})
    assert r.status_code == 200 and await _col(db, "SELECT fit_override FROM leads WHERE id=?", lid) == "qualified"
    assert (await client.put(f"/api/leads/{lid}/qualification", json={"override": "maybe"})).status_code == 422
    assert (await client.put(f"/api/leads/{lid}/qualification", json={"override": None})).status_code == 200
    assert await _col(db, "SELECT fit_override FROM leads WHERE id=?", lid) is None
    assert (await client.put(f"/api/campaigns/{cid}/min-fit", json={"min_fit_score": 4})).status_code == 200
    assert await _col(db, "SELECT min_fit_score FROM campaigns WHERE id=?", cid) == 4
    assert (await client.put(f"/api/campaigns/{cid}/min-fit", json={"min_fit_score": 9})).status_code == 422


async def test_rate_endpoints_queue_and_run(db, client, llm):
    cid = await _campaign(db)
    a = await _lead(db, cid, "a.com")
    b = await _lead(db, cid, "b.com", score=5)
    r = await client.post(f"/api/campaigns/{cid}/rate")
    assert r.json() == {"queued": 1} and await _col(db, "SELECT fit_score FROM leads WHERE id=?", a) == 2
    r = await client.post("/api/leads/rate", json={"lead_ids": [b], "rerate": True})
    assert r.json() == {"queued": 1} and await _col(db, "SELECT fit_score FROM leads WHERE id=?", b) == 2


async def test_lists_report_fit_counts_and_disqualified_flag(db, client):
    cid = await _campaign(db)
    await _lead(db, cid, "good.com", score=4)
    await _lead(db, cid, "bad.com", score=1)
    await _lead(db, cid, "new.com")
    c = [x for x in (await client.get("/api/campaigns")).json() if x["id"] == cid][0]
    assert (c["fit_qualified"], c["fit_disqualified"], c["fit_unrated"], c["min_fit_score"]) == (1, 1, 1, 3)
    leads = (await client.get(f"/api/leads?campaign_id={cid}")).json()
    assert [(l["company_domain"], l["disqualified"]) for l in leads] == [
        ("good.com", 0), ("new.com", 0), ("bad.com", 1)]  # best first, disqualified last


async def test_launch_says_when_every_remaining_lead_is_disqualified(db, client):
    cid = await _campaign(db, min_fit=4)
    await _lead(db, cid, "low.com", score=2, body="b")
    await _lead(db, cid, "mid.com", score=3, body="b")
    r = (await client.post(f"/api/campaigns/{cid}/launch")).json()
    assert r["status"] == "all_disqualified" and r["dispatched"] == 0
    assert "2 remaining leads" in r["message"] and "below 4" in r["message"]
    assert "Disqualify below" in r["message"]
