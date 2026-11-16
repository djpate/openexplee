"""Turns campaign pool rows into leads under the real-emails-only rule."""
import asyncio
import json
import re
from typing import Optional

from app.config import settings
from app.database import get_db_connection
from app.engine.company_pool import refresh_pool_counters
from app.engine.discovery import _extract_people_with_llm
from app.engine.scraper import scrape_company_website
from app.engine.site_check import analyze_site
from app.engine.fit import rate_lead_fit
from app.engine.verifier import enrich_person_email, verify_email_smtp

# Bot walls: the site is alive but refuses scrapers, so only the Overture emails count.
BLOCKED_ERRORS = frozenset({"HTTP 401", "HTTP 403", "HTTP 429"})
# A guess that lands on a role mailbox was never found anywhere: it is the banned contact@ fallback.
GENERIC_LOCAL_PARTS = frozenset({"contact", "hello", "info", "office", "admin", "sales", "support", "team", "mail"})
MAX_FOUND_TO_VERIFY = 3
LEAD_COLUMNS = (
    "campaign_id", "company_name", "company_domain", "website_url",
    "contact_name", "first_name", "last_name", "job_title",
    "email", "verification_status", "verification_reason",
    "personalized_subject", "personalized_body", "status", "phone", "company_id",
    "company_summary", "site_facts", "fit_score", "fit_reason",
)


def pick_lead_email(person: Optional[dict], found: list[tuple[str, str]]) -> tuple[Optional[str], str, str]:
    """Apply the lead rule. `person` is a guessed address result, `found` the checked own-domain addresses.

    Order: a `verified` guess, then the first found address that isn't `invalid`, then a `deliverable`
    guess (the mailbox was never confirmed). Returns (email, verification_status, "") for a lead, or
    (None, "", skip_reason).
    """
    guess = person.get("email") if person else None
    if guess and guess.split("@")[0].lower() in GENERIC_LOCAL_PARTS:
        guess = None
    if guess and person.get("status") == "verified":
        return guess, "verified", ""
    for email, status in found:
        if status != "invalid":
            return email, status, ""
    if guess and person.get("status") == "deliverable":
        return guess, "deliverable", ""
    return None, "", "email_invalid" if found else "no_email"


def _ascii_letters(name: Optional[str]) -> str:
    """What the verifier keeps of a name when it builds permutations."""
    return re.sub(r"[^a-zA-Z]", "", name or "")


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _own_domain_emails(emails, domain: str) -> list[str]:
    out: list[str] = []
    for raw in emails:
        email = str(raw).strip().lower()
        if (email.endswith("@" + domain) or email.endswith("." + domain)) and email not in out:
            out.append(email)
    return out


async def enrich_company(company: dict, value_prop: str) -> dict:
    """Scrape, find and check emails, and build the lead for one pool row (no email draft).

    Returns {"lead": {...}} or {"skip_reason": str}.
    """
    domain = company["domain"]
    intel = await scrape_company_website(company.get("website_url") or domain)
    # The key, not its text: an httpx timeout can stringify to "".
    failed = "error" in intel
    if failed and intel["error"] not in BLOCKED_ERRORS:
        return {"skip_reason": "site_unreachable"}
    site_emails = [] if failed else list(intel.get("found_emails") or [])
    text = "" if failed else (intel.get("content_summary") or "")

    found = _own_domain_emails(site_emails + _json_list(company.get("emails")), domain)
    checked: list[tuple[str, str]] = []
    reasons: dict[tuple[str, str], str] = {}
    for email in found[:MAX_FOUND_TO_VERIFY]:
        res = await verify_email_smtp(email, timeout=5.0)
        checked.append((email, res.get("status", "unverified")))
        reasons[checked[-1]] = res.get("details", "")
        if checked[-1][1] != "invalid":
            break

    people = await _extract_people_with_llm(domain, text) if text else []
    person = people[0] if people else None
    guess = None
    # With no ASCII letters in a name, the verifier's permutations fall back to contact@/hello@/info@.
    if person and _ascii_letters(person.get("first_name")) and _ascii_letters(person.get("last_name")):
        guess = await enrich_person_email(person["first_name"], person["last_name"], domain)
        reasons.setdefault((guess.get("email"), guess.get("status")), guess.get("details", ""))

    email, status, skip_reason = pick_lead_email(guess, checked)
    if skip_reason:
        return {"skip_reason": skip_reason}

    # The first person found names the lead even when the email is a role address like info@.
    first = last = contact_name = job_title = None
    if person:
        first, last = person.get("first_name"), person.get("last_name")
        contact_name = f"{first} {last}".strip()
        job_title = person.get("job_title") or None
    company_name = company.get("name") or domain
    # A bot-blocked site gives no text; the pool row still says what and where the business is.
    place = (company.get("category"), company.get("city"), company.get("region"))
    # Drafting happens later (mass action or at send time); keep what the draft will need.
    summary = text[:1000] or ", ".join(x for x in place if x)
    phones = _json_list(company.get("phones"))
    facts = analyze_site(intel)
    # How much they likely need what this campaign offers (cheap bulk model); None if unrated.
    fit_score, fit_reason = await rate_lead_fit(company_name, domain, summary, facts.get("lines"), value_prop)
    return {"lead": {
        "campaign_id": company.get("campaign_id"),
        "company_name": company_name,
        "company_domain": domain,
        "website_url": company.get("website_url") or intel.get("url"),
        "contact_name": contact_name,
        "first_name": first,
        "last_name": last,
        "job_title": job_title,
        "email": email,
        "verification_status": status,
        "verification_reason": reasons.get((email, status), ""),
        "personalized_subject": None,
        "personalized_body": None,
        "status": "enriched",
        "phone": phones[0] if phones else None,
        "company_id": company.get("id"),
        "company_summary": summary or None,
        "site_facts": json.dumps(facts),
        "fit_score": fit_score,
        "fit_reason": fit_reason or None,
    }}


async def claim_pending(campaign_id: int, n: int) -> list[dict]:
    """Atomically move the next n pending rows to 'processing' and return them."""
    if n <= 0:
        return []
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "UPDATE campaign_companies SET status = 'processing' WHERE id IN ("
            "SELECT id FROM campaign_companies WHERE campaign_id = ? AND status = 'pending' "
            "ORDER BY tier, confidence DESC, id LIMIT ?) RETURNING *", (campaign_id, n))
        rows = [dict(r) for r in await cur.fetchall()]
        await conn.commit()
    finally:
        await conn.close()
    return sorted(rows, key=lambda r: (r["tier"] or 0, -(r["confidence"] or 0), r["id"]))


async def _gate(company: dict) -> Optional[dict]:
    """The last check before enriching. Returns a ready result, or None to go ahead.

    {"lead_id": id} when this campaign already has a lead for the domain (e.g. from before the pool);
    {"skip_reason": "in_other_campaign"} when another campaign has a lead for it, or an earlier live
    pool row for it.
    """
    domain, cid = company["domain"], company["campaign_id"]
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "SELECT id FROM leads WHERE campaign_id = ? AND LOWER(company_domain) = ? ORDER BY id LIMIT 1",
            (cid, domain))
        own = await cur.fetchone()
        if own:
            return {"lead_id": own["id"]}
        cur = await conn.execute(
            "SELECT 1 FROM leads WHERE LOWER(company_domain) = ? "
            "AND (campaign_id IS NULL OR campaign_id != ?) "
            "UNION ALL "
            "SELECT 1 FROM campaign_companies WHERE domain = ? AND campaign_id != ? "
            "AND ((status IN ('pending', 'processing', 'done') AND id < ?) OR skip_reason = 'deleted_by_user') "
            "LIMIT 1",
            (domain, cid, domain, cid, company["id"]))
        return {"skip_reason": "in_other_campaign"} if await cur.fetchone() else None
    finally:
        await conn.close()


async def _finish_row(company: dict, result: dict) -> dict:
    """Write the lead (if any), the row outcome and the campaign lead count in one transaction.

    Returns {"new_lead_id": id}, {"existing_lead_id": id} or {"skip_reason": str}.
    """
    conn = await get_db_connection()
    try:
        lead = result.get("lead")
        if lead:
            cur = await conn.execute(
                f"INSERT INTO leads ({', '.join(LEAD_COLUMNS)}) VALUES ({', '.join('?' for _ in LEAD_COLUMNS)})",
                tuple(lead[c] for c in LEAD_COLUMNS))
            outcome = {"new_lead_id": cur.lastrowid}
            await conn.execute("UPDATE campaign_companies SET status = 'done', skip_reason = NULL, lead_id = ? "
                               "WHERE id = ?", (cur.lastrowid, company["id"]))
            await conn.execute("UPDATE campaigns SET total_leads = (SELECT COUNT(*) FROM leads WHERE campaign_id = ?) "
                               "WHERE id = ?", (company["campaign_id"], company["campaign_id"]))
        elif result.get("lead_id"):
            outcome = {"existing_lead_id": result["lead_id"]}
            await conn.execute("UPDATE campaign_companies SET status = 'done', skip_reason = NULL, lead_id = ? "
                               "WHERE id = ?", (result["lead_id"], company["id"]))
        else:
            outcome = {"skip_reason": result["skip_reason"]}
            await conn.execute("UPDATE campaign_companies SET status = 'skipped', skip_reason = ? WHERE id = ?",
                               (result["skip_reason"], company["id"]))
        await conn.commit()
        return outcome
    finally:
        await conn.close()


async def _release(company: dict) -> None:
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE campaign_companies SET status = 'pending' WHERE id = ? AND status = 'processing'",
                           (company["id"],))
        await conn.commit()
    finally:
        await conn.close()


async def _process_row(company: dict, value_prop: str, sem: asyncio.Semaphore) -> Optional[dict]:
    async with sem:
        try:
            result = await _gate(company) or await enrich_company(company, value_prop)
            outcome = await _finish_row(company, result)
        except Exception as e:
            # Not a skip: put the row back so a later batch retries it.
            print(f"[LeadBuilder] {company.get('domain')} failed, returned to pending: {type(e).__name__}: {e}")
            try:
                await _release(company)
            except Exception as re_err:
                print(f"[LeadBuilder] could not release {company.get('domain')}: {re_err}")
            outcome = None
        # Every finished row moves the pool counters, so the UI is current mid-batch.
        await refresh_pool_counters(company["campaign_id"])
        return outcome


async def process_next_batch(campaign_id: int, batch_size: int, value_prop: str) -> dict:
    """Claim the next batch of pending rows and turn them into leads in parallel."""
    rows = await claim_pending(campaign_id, batch_size)
    sem = asyncio.Semaphore(max(1, settings.discovery_concurrency))
    outcomes = await asyncio.gather(*(_process_row(r, value_prop, sem) for r in rows))
    await refresh_pool_counters(campaign_id)
    done = [o for o in outcomes if o is not None]
    lead_ids = [o["new_lead_id"] for o in done if "new_lead_id" in o]
    skipped = sum(1 for o in done if "skip_reason" in o)
    return {"processed": len(done), "leads": len(lead_ids), "skipped": skipped, "lead_ids": lead_ids}
