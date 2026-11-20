import asyncio
import json
from collections import Counter
from unittest.mock import ANY

import pytest

from app.config import settings
from app.database import get_db_connection
from app.engine import lead_builder
from app.engine.company_pool import normalize_domain, save_pool
from app.engine.lead_builder import enrich_company, pick_lead_email, process_next_batch


# ---------- the lead rule ----------

def test_person_guess_needs_deliverable():
    assert pick_lead_email({"email": "jo@a.com", "status": "deliverable"}, []) == ("jo@a.com", "deliverable", "")
    assert pick_lead_email({"email": "jo@a.com", "status": "verified"}, []) == ("jo@a.com", "verified", "")
    assert pick_lead_email({"email": "jo@a.com", "status": "catch_all"}, []) == (None, "", "no_email")
    assert pick_lead_email({"email": "jo@a.com", "status": "risky"}, []) == (None, "", "no_email")


def test_found_address_accepted_when_risky_rejected_when_invalid():
    assert pick_lead_email(None, [("info@a.com", "risky")]) == ("info@a.com", "risky", "")
    assert pick_lead_email(None, [("info@a.com", "catch_all")]) == ("info@a.com", "catch_all", "")
    assert pick_lead_email(None, [("info@a.com", "invalid")]) == (None, "", "email_invalid")


def test_first_found_address_that_is_not_invalid_wins():
    found = [("info@a.com", "invalid"), ("office@a.com", "risky"), ("hi@a.com", "verified")]
    assert pick_lead_email({"email": "jo@a.com", "status": "invalid"}, found) == ("office@a.com", "risky", "")


def test_verified_person_preferred_over_found_role_address():
    assert pick_lead_email({"email": "jo@a.com", "status": "verified"}, [("info@a.com", "risky")])[0] == "jo@a.com"


def test_deliverable_guess_loses_to_found_address():
    guess = {"email": "jo@a.com", "status": "deliverable"}
    assert pick_lead_email(guess, [("info@a.com", "risky")]) == ("info@a.com", "risky", "")
    assert pick_lead_email(guess, [("info@a.com", "invalid")]) == ("jo@a.com", "deliverable", "")
    assert pick_lead_email(guess, []) == ("jo@a.com", "deliverable", "")


@pytest.mark.parametrize("local", ["contact", "hello", "info", "office", "admin", "sales", "support", "team", "mail",
                                   "Contact"])
@pytest.mark.parametrize("status", ["verified", "deliverable"])
def test_generic_guessed_address_never_accepted(local, status):
    assert pick_lead_email({"email": f"{local}@a.com", "status": status}, []) == (None, "", "no_email")
    assert pick_lead_email({"email": f"{local}@a.com", "status": status}, [("x@a.com", "invalid")]) == \
        (None, "", "email_invalid")


def test_no_contact_fallback():
    assert pick_lead_email(None, []) == (None, "", "no_email")


# ---------- fakes for the network and LLM ----------

class Fakes:
    """Stands in for the scraper, SMTP, people extraction and email guessing."""

    def __init__(self, monkeypatch, scrape_delay=0.0):
        self.sites = {}    # domain -> overrides for the scrape result
        self.smtp = {}     # email -> verification status (default "risky")
        self.people = {}   # domain -> extracted people
        self.guess = {}    # domain -> enrich_person_email result
        self.scraped, self.verified, self.extracted, self.guessed, self.rated = [], [], [], [], []
        self.scrape_delay = scrape_delay
        monkeypatch.setattr(lead_builder, "scrape_company_website", self.scrape)
        monkeypatch.setattr(lead_builder, "verify_email_smtp", self.verify)
        monkeypatch.setattr(lead_builder, "_extract_people_with_llm", self.extract)
        monkeypatch.setattr(lead_builder, "enrich_person_email", self.enrich_person)
        monkeypatch.setattr(lead_builder, "rate_lead_fit", self.rate)

    async def scrape(self, url):
        domain = normalize_domain(url)
        self.scraped.append(domain)
        if self.scrape_delay:
            await asyncio.sleep(self.scrape_delay)
        intel = {"domain": domain, "url": f"https://{domain}", "title": "", "description": "",
                 "content_summary": f"About {domain}", "found_emails": [f"info@{domain}"], "subpages": []}
        intel.update(self.sites.get(domain, {}))
        return intel

    async def verify(self, email, timeout=2.0):
        self.verified.append((email, timeout))
        return {"email": email, "status": self.smtp.get(email, "risky"), "details": f"smtp says {email}"}

    async def extract(self, domain, text, job_titles=None):
        self.extracted.append((domain, text))
        return self.people.get(domain, [])

    async def rate(self, company_name, company_domain, summary, site_facts, value_proposition, target=""):
        self.rated.append((company_domain, value_proposition, target))
        return None, ""

    async def enrich_person(self, first, last, domain):
        self.guessed.append((first, last, domain))
        return self.guess.get(domain) or {"email": f"{first}.{last}@{domain}".lower(), "status": "invalid",
                                          "details": "No deliverable permutation found."}


def row(domain="a.com", emails=(), phones=("+1 813 555 0100",), **kw):
    r = {"id": 7, "campaign_id": 1, "source": "overture", "source_id": "s1", "name": "A Dental",
         "category": "dentist", "domain": domain, "website_url": f"https://www.{domain}/",
         "emails": json.dumps(list(emails)), "phones": json.dumps(list(phones)), "city": "Tampa",
         "region": "FL", "country": "US", "lat": 1.0, "lon": 2.0, "tier": 1, "confidence": 0.9,
         "status": "processing", "skip_reason": None, "lead_id": None}
    r.update(kw)
    return r


JO = {"first_name": "Jo", "last_name": "Smith", "job_title": "Owner"}


# ---------- enrich_company ----------

@pytest.mark.parametrize("error", ["HTTP 503", "HTTP 404", "HTTP 410", "HTTP 500",
                                   "[Errno -2] Name or service not known", ""])  # httpx timeouts stringify to ""
async def test_unreachable_site_is_skipped(monkeypatch, error):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"error": error, "found_emails": [], "content_summary": ""}
    assert await enrich_company(row(emails=["office@a.com"]), "vp") == {"skip_reason": "site_unreachable"}
    assert f.verified == [] and f.extracted == []


@pytest.mark.parametrize("error", ["HTTP 401", "HTTP 403", "HTTP 429"])
async def test_bot_blocked_site_continues_with_overture_emails_only(monkeypatch, error):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"error": error, "found_emails": ["info@a.com"], "content_summary": "Jo Smith, CEO"}
    out = await enrich_company(row(emails=["office@a.com"]), "vp")
    assert out["lead"]["email"] == "office@a.com"
    assert [e for e, _ in f.verified] == ["office@a.com"]
    assert f.extracted == []


async def test_bot_blocked_site_summary_uses_pool_category_and_place(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"error": "HTTP 403", "found_emails": set(), "content_summary": ""}
    lead = (await enrich_company(row(emails=["office@a.com"]), "vp"))["lead"]
    assert lead["company_summary"] == "dentist, Tampa, FL"


async def test_bot_blocked_site_without_overture_email_is_no_email(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"error": "HTTP 403", "found_emails": set(), "content_summary": ""}
    assert await enrich_company(row(), "vp") == {"skip_reason": "no_email"}


async def test_found_emails_limited_to_own_domain_plus_overture(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": ["info@a.com", "x@gmail.com"]}
    f.smtp["info@a.com"] = "invalid"
    out = await enrich_company(row(emails=["office@a.com", "owner@gmail.com", "Info@A.com"]), "vp")
    assert out["lead"]["email"] == "office@a.com"
    assert out["lead"]["verification_status"] == "risky"
    assert f.verified == [("info@a.com", 5.0), ("office@a.com", 5.0)]


async def test_subdomain_address_kept_and_lookalike_domain_dropped(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": ["x@nota.com", "sales@mail.a.com"]}
    out = await enrich_company(row(), "vp")
    assert out["lead"]["email"] == "sales@mail.a.com"
    assert [e for e, _ in f.verified] == ["sales@mail.a.com"]


async def test_verifies_at_most_three_found_addresses(monkeypatch):
    f = Fakes(monkeypatch)
    found = [f"r{i}@a.com" for i in range(5)]
    f.sites["a.com"] = {"found_emails": found}
    f.smtp.update({e: "invalid" for e in found})
    assert await enrich_company(row(), "vp") == {"skip_reason": "email_invalid"}
    assert [e for e, _ in f.verified] == found[:3]


async def test_stops_verifying_at_first_found_address_that_is_not_invalid(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": ["info@a.com"]}
    out = await enrich_company(row(emails=["office@a.com"]), "vp")
    assert out["lead"]["email"] == "info@a.com"
    assert [e for e, _ in f.verified] == ["info@a.com"]


async def test_verified_person_wins_and_names_the_lead(monkeypatch):
    f = Fakes(monkeypatch)
    f.people["a.com"] = [JO, {"first_name": "Al", "last_name": "Ng", "job_title": "Manager"}]
    f.guess["a.com"] = {"email": "jo.smith@a.com", "status": "verified", "details": "Mailbox exists."}
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert (lead["email"], lead["verification_status"], lead["verification_reason"]) == \
        ("jo.smith@a.com", "verified", "Mailbox exists.")
    assert (lead["contact_name"], lead["first_name"], lead["last_name"], lead["job_title"]) == \
        ("Jo Smith", "Jo", "Smith", "Owner")
    assert f.guessed == [("Jo", "Smith", "a.com")]
    assert f.extracted == [("a.com", "About a.com")]


async def test_reason_matches_the_check_that_chose_the_email(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": ["jo@a.com"]}  # found risky, then guessed and verified
    f.people["a.com"] = [JO]
    f.guess["a.com"] = {"email": "jo@a.com", "status": "verified", "details": "Mailbox exists."}
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert (lead["email"], lead["verification_status"], lead["verification_reason"]) == \
        ("jo@a.com", "verified", "Mailbox exists.")


async def test_role_address_lead_still_greets_first_person(monkeypatch):
    f = Fakes(monkeypatch)
    f.people["a.com"] = [JO]
    f.guess["a.com"] = {"email": "jo@a.com", "status": "catch_all", "details": "Catch-all."}
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert (lead["email"], lead["verification_status"], lead["verification_reason"]) == \
        ("info@a.com", "risky", "smtp says info@a.com")
    assert (lead["contact_name"], lead["first_name"], lead["last_name"], lead["job_title"]) == \
        ("Jo Smith", "Jo", "Smith", "Owner")


async def test_no_person_leaves_contact_empty(monkeypatch):
    f = Fakes(monkeypatch)
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert (lead["contact_name"], lead["first_name"], lead["last_name"], lead["job_title"]) == (None, None, None, None)
    assert f.guessed == []


@pytest.mark.parametrize("first,last", [("Иван", "Петров"), ("Jo", "李"), ("Иван", "Smith"), ("Jo", "")])
async def test_name_without_ascii_letters_is_not_guessed(monkeypatch, first, last):
    f = Fakes(monkeypatch)
    f.people["a.com"] = [{"first_name": first, "last_name": last, "job_title": "Owner"}]
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert f.guessed == []
    assert lead["email"] == "info@a.com"
    assert lead["contact_name"] == f"{first} {last}".strip()


async def test_guess_landing_on_generic_address_is_not_a_lead(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": []}
    f.people["a.com"] = [JO]
    f.guess["a.com"] = {"email": "contact@a.com", "status": "deliverable", "details": "greylisted"}
    assert await enrich_company(row(), "vp") == {"skip_reason": "no_email"}


async def test_deliverable_guess_loses_to_found_role_address(monkeypatch):
    f = Fakes(monkeypatch)
    f.people["a.com"] = [JO]
    f.guess["a.com"] = {"email": "jo.smith@a.com", "status": "deliverable", "details": "MX answered 451"}
    lead = (await enrich_company(row(), "vp"))["lead"]
    assert (lead["email"], lead["verification_status"]) == ("info@a.com", "risky")
    assert lead["contact_name"] == "Jo Smith"


async def test_catch_all_guess_alone_is_not_a_lead(monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": []}
    f.people["a.com"] = [JO]
    f.guess["a.com"] = {"email": "jo@a.com", "status": "catch_all", "details": "Catch-all."}
    assert await enrich_company(row(), "vp") == {"skip_reason": "no_email"}


async def test_discovery_leaves_leads_undrafted_with_a_summary(monkeypatch):
    f = Fakes(monkeypatch)
    lead = (await enrich_company(row("b.com", name="B Dental"), "our value"))["lead"]
    assert (lead["personalized_subject"], lead["personalized_body"]) == (None, None)
    assert lead["company_summary"] == "About b.com"


async def test_lead_carries_pool_fields(monkeypatch):
    Fakes(monkeypatch)
    lead = (await enrich_company(row(id=42, campaign_id=3, phones=["+1 813 555 0199", "+1 1"]), "vp"))["lead"]
    assert lead["campaign_id"] == 3
    assert lead["company_id"] == 42
    assert lead["company_name"] == "A Dental"
    assert lead["company_domain"] == "a.com"
    assert lead["website_url"] == "https://www.a.com/"
    assert lead["phone"] == "+1 813 555 0199"
    assert lead["status"] == "enriched"


# ---------- batches ----------

async def _campaign(name="c"):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("INSERT INTO campaigns (name, icp_prompt) VALUES (?, 'dentists in Tampa')", (name,))
        await conn.commit()
        return cur.lastrowid
    finally:
        await conn.close()


async def _pool(cid, domains, emails=None):
    emails = emails or {}
    await save_pool(cid, [{"domain": d, "website_url": f"https://{d}", "name": d.split(".")[0].title(),
                           "emails": emails.get(d, []), "phones": ["+1 813 555 0100"], "tier": 1,
                           "confidence": 0.9} for d in domains], "overture")


async def _fetch(sql, *args):
    conn = await get_db_connection()
    try:
        cur = await conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def _rows(cid):
    rows = await _fetch("SELECT domain, status, skip_reason FROM campaign_companies WHERE campaign_id = ?", cid)
    return {r["domain"]: (r["status"], r["skip_reason"]) for r in rows}


async def test_batch_rates_leads_against_the_offer_and_the_target(db, monkeypatch):
    f = Fakes(monkeypatch)
    cid = await _campaign()
    await _pool(cid, ["a.com"])
    await process_next_batch(cid, 1, "our offer", "dentists in Tampa")
    assert f.rated == [("a.com", "our offer", "dentists in Tampa")]


async def test_batch_marks_rows_and_counts(db, monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["d.com"] = {"error": "HTTP 503", "found_emails": [], "content_summary": ""}
    f.sites["e.com"] = {"found_emails": []}
    cid = await _campaign()
    await _pool(cid, ["a.com", "b.com", "c.com", "d.com", "e.com"])

    out = await process_next_batch(cid, 5, "vp")

    assert out == {"processed": 5, "leads": 3, "skipped": 2, "lead_ids": ANY}
    assert await _rows(cid) == {"a.com": ("done", None), "b.com": ("done", None), "c.com": ("done", None),
                                "d.com": ("skipped", "site_unreachable"), "e.com": ("skipped", "no_email")}
    linked = await _fetch("SELECT cc.id AS row_id, cc.lead_id, l.id, l.company_id, l.campaign_id, l.status, l.phone "
                          "FROM campaign_companies cc JOIN leads l ON l.id = cc.lead_id WHERE cc.status = 'done'")
    assert sorted(r["lead_id"] for r in linked) == sorted(out["lead_ids"])
    assert all(r["company_id"] == r["row_id"] and r["campaign_id"] == cid and r["status"] == "enriched"
               and r["phone"] == "+1 813 555 0100" for r in linked)
    camp = (await _fetch("SELECT total_leads, pool_total, pool_pending FROM campaigns WHERE id = ?", cid))[0]
    assert camp == {"total_leads": 3, "pool_total": 5, "pool_pending": 0}


async def test_batch_takes_only_batch_size_rows(db, monkeypatch):
    Fakes(monkeypatch)
    cid = await _campaign()
    await _pool(cid, ["a.com", "b.com", "c.com"])
    out = await process_next_batch(cid, 2, "vp")
    assert out["processed"] == 2
    statuses = Counter(s for s, _ in (await _rows(cid)).values())
    assert statuses == {"done": 2, "pending": 1}
    camp = (await _fetch("SELECT pool_pending FROM campaigns WHERE id = ?", cid))[0]
    assert camp["pool_pending"] == 1


async def test_progress_written_as_each_row_finishes(db, monkeypatch):
    Fakes(monkeypatch)
    monkeypatch.setattr(settings, "discovery_concurrency", 1)
    cid = await _campaign()
    await _pool(cid, ["a.com", "b.com", "c.com"])
    seen = []
    real = lead_builder.enrich_company

    async def watching(company, value_prop, target=""):
        camp = (await _fetch("SELECT total_leads FROM campaigns WHERE id = ?", cid))[0]
        done = await _fetch("SELECT COUNT(*) AS n FROM campaign_companies WHERE status = 'done'")
        seen.append((camp["total_leads"], done[0]["n"]))
        return await real(company, value_prop, target)

    monkeypatch.setattr(lead_builder, "enrich_company", watching)
    await process_next_batch(cid, 3, "vp")
    assert seen == [(0, 0), (1, 1), (2, 2)]


async def test_pool_counters_refreshed_as_each_row_finishes(db, monkeypatch):
    f = Fakes(monkeypatch)
    f.sites["a.com"] = {"found_emails": []}  # skip
    f.sites["c.com"] = {"found_emails": []}  # skip
    monkeypatch.setattr(settings, "discovery_concurrency", 1)
    cid = await _campaign()
    await _pool(cid, ["a.com", "b.com", "c.com", "d.com", "e.com"])
    seen = []
    real = lead_builder.enrich_company

    async def watching(company, value_prop, target=""):
        seen.append((await _fetch("SELECT pool_pending FROM campaigns WHERE id = ?", cid))[0]["pool_pending"])
        return await real(company, value_prop, target)

    monkeypatch.setattr(lead_builder, "enrich_company", watching)
    out = await process_next_batch(cid, 3, "vp")

    assert (out["leads"], out["skipped"]) == (1, 2)
    # Rows b and c start after a skip and a lead have finished: the counter is already current.
    assert seen[1:] == [2, 2]
    assert (await _fetch("SELECT pool_pending FROM campaigns WHERE id = ?", cid))[0]["pool_pending"] == 2


async def test_domain_already_a_lead_in_own_campaign_links_existing_lead(db, monkeypatch):
    f = Fakes(monkeypatch)
    cid = await _campaign()
    conn = await get_db_connection()
    try:
        cur = await conn.execute("INSERT INTO leads (campaign_id, company_name, company_domain, email, status) "
                                 "VALUES (?, 'A', 'a.com', 'old@a.com', 'sent')", (cid,))
        old_id = cur.lastrowid
        await conn.commit()
    finally:
        await conn.close()
    await _pool(cid, ["a.com", "b.com"])

    out = await process_next_batch(cid, 10, "vp")

    assert out == {"processed": 2, "leads": 1, "skipped": 0, "lead_ids": ANY}
    assert old_id not in out["lead_ids"]
    rows = await _fetch("SELECT domain, status, skip_reason, lead_id FROM campaign_companies "
                        "WHERE campaign_id = ?", cid)
    assert {r["domain"]: (r["status"], r["skip_reason"], r["lead_id"]) for r in rows} == {
        "a.com": ("done", None, old_id), "b.com": ("done", None, out["lead_ids"][0])}
    assert f.scraped == ["b.com"]
    leads = await _fetch("SELECT company_domain FROM leads WHERE campaign_id = ? ORDER BY id", cid)
    assert [r["company_domain"] for r in leads] == ["a.com", "b.com"]


async def test_concurrency_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "discovery_concurrency", 3)
    cid = await _campaign()
    await _pool(cid, [f"d{i}.com" for i in range(20)])
    in_flight, peak = 0, 0

    async def fake_enrich(company, value_prop, target=""):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"skip_reason": "no_email"}

    monkeypatch.setattr(lead_builder, "enrich_company", fake_enrich)
    out = await process_next_batch(cid, 20, "vp")
    assert out["processed"] == 20
    assert peak == 3


@pytest.mark.parametrize("size_a,size_b", [(10, 10), (6, 6)])
async def test_overlapping_batches_never_double_process(db, monkeypatch, size_a, size_b):
    f = Fakes(monkeypatch, scrape_delay=0.01)
    f.sites.update({f"d{i}.com": {"found_emails": []} for i in range(0, 10, 3)})  # some skip, most become leads
    cid = await _campaign()
    await _pool(cid, [f"d{i}.com" for i in range(10)])
    enrich_calls = Counter()
    real = lead_builder.enrich_company

    async def counting(company, value_prop, target=""):
        enrich_calls[company["domain"]] += 1
        return await real(company, value_prop, target)

    monkeypatch.setattr(lead_builder, "enrich_company", counting)
    a, b = await asyncio.gather(process_next_batch(cid, size_a, "vp"), process_next_batch(cid, size_b, "vp"))

    assert a["processed"] + b["processed"] == 10
    assert a["processed"] <= size_a and b["processed"] <= size_b
    assert len(enrich_calls) == 10 and set(enrich_calls.values()) == {1}
    assert Counter(f.scraped) == Counter({f"d{i}.com": 1 for i in range(10)})
    leads = await _fetch("SELECT company_domain FROM leads WHERE campaign_id = ?", cid)
    assert len(leads) == a["leads"] + b["leads"] == 6
    assert len({r["company_domain"] for r in leads}) == 6
    assert {s for s, _ in (await _rows(cid)).values()} == {"done", "skipped"}


async def test_first_campaign_wins_shared_domain(db, monkeypatch):
    f = Fakes(monkeypatch)
    c1, c2 = await _campaign("one"), await _campaign("two")
    await _pool(c1, ["shared.com"])
    await _pool(c2, ["shared.com", "own.com"])

    out = await process_next_batch(c2, 10, "vp")

    assert out == {"processed": 2, "leads": 1, "skipped": 1, "lead_ids": ANY}
    assert await _rows(c2) == {"shared.com": ("skipped", "in_other_campaign"), "own.com": ("done", None)}
    assert f.scraped == ["own.com"]


async def test_later_pool_row_does_not_block_first_campaign_and_lead_holds_domain(db, monkeypatch):
    f = Fakes(monkeypatch)
    c1, c2 = await _campaign("one"), await _campaign("two")
    await _pool(c1, ["shared.com"])
    await _pool(c2, ["shared.com"])

    assert (await process_next_batch(c1, 10, "vp"))["leads"] == 1
    assert await _rows(c1) == {"shared.com": ("done", None)}

    # Campaign 1 now holds it as a lead, so campaign 2 skips it.
    assert (await process_next_batch(c2, 10, "vp"))["skipped"] == 1
    assert await _rows(c2) == {"shared.com": ("skipped", "in_other_campaign")}
    assert f.scraped == ["shared.com"]


async def test_skipped_row_in_other_campaign_does_not_hold_domain(db, monkeypatch):
    Fakes(monkeypatch)
    c1, c2 = await _campaign("one"), await _campaign("two")
    await _pool(c1, ["shared.com"])
    await _pool(c2, ["shared.com"])
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE campaign_companies SET status = 'skipped', skip_reason = 'no_email' "
                           "WHERE campaign_id = ?", (c1,))
        await conn.commit()
    finally:
        await conn.close()
    assert (await process_next_batch(c2, 10, "vp"))["leads"] == 1


async def test_unexpected_error_returns_row_to_pending(db, monkeypatch, capsys):
    f = Fakes(monkeypatch)
    cid = await _campaign()
    await _pool(cid, ["a.com", "boom.com"])
    real = lead_builder.enrich_company

    async def flaky(company, value_prop, target=""):
        if company["domain"] == "boom.com":
            raise RuntimeError("llm exploded")
        return await real(company, value_prop, target)

    monkeypatch.setattr(lead_builder, "enrich_company", flaky)
    out = await process_next_batch(cid, 10, "vp")

    assert out == {"processed": 1, "leads": 1, "skipped": 0, "lead_ids": ANY}
    assert await _rows(cid) == {"a.com": ("done", None), "boom.com": ("pending", None)}
    assert "boom.com" in capsys.readouterr().out
    camp = (await _fetch("SELECT pool_pending FROM campaigns WHERE id = ?", cid))[0]
    assert camp["pool_pending"] == 1


async def test_lead_stores_site_facts_from_the_pages_read(monkeypatch):
    import json
    f = Fakes(monkeypatch)
    f.sites["b.com"] = {"final_url": "https://b.com/",
                        "pages": [{"url": "https://b.com/", "html": '<iframe src="https://fareharbor.com/x">', "kind": "home"}]}
    lead = (await enrich_company(row("b.com"), "vp"))["lead"]
    facts = json.loads(lead["site_facts"])
    assert facts["booking_tools"] == ["FareHarbor"] and facts["lines"]
