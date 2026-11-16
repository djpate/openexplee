"""Draft cold emails for leads on demand: from a mass action or just before sending.

Discovery no longer drafts, so the Claude cost of a draft is only paid for leads someone
actually wants to email.
"""
import asyncio
import json
from typing import Dict, Iterable, List, Optional

from app.config import settings
from app.database import get_db_connection
from app.engine.copywriter import generate_cold_email
from app.engine.scraper import scrape_company_website
from app.engine.site_check import analyze_site

# Leads being drafted right now, by lead id -> campaign id. Overlapping requests skip these.
_IN_FLIGHT: Dict[int, int] = {}

# Leads that may still be emailed: same set the sender picks from.
SENDABLE = (
    "status IN ('enriched', 'queued', 'discovered') AND verification_status != 'invalid' "
    "AND email IS NOT NULL AND email != ''"
)
UNDRAFTED = "(personalized_body IS NULL OR personalized_body = '')"


def drafting_count(campaign_id: int) -> int:
    return sum(1 for cid in _IN_FLIGHT.values() if cid == campaign_id)


async def undrafted_lead_ids(campaign_id: int) -> List[int]:
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            f"SELECT id FROM leads WHERE campaign_id = ? AND {SENDABLE} AND {UNDRAFTED} ORDER BY id",
            (campaign_id,),
        )
        return [r["id"] for r in await cur.fetchall()]
    finally:
        await conn.close()


def _draftable(overwrite: bool) -> str:
    """Leads a draft request may touch: sendable ones, and only undrafted ones unless overwriting."""
    return SENDABLE if overwrite else f"{SENDABLE} AND {UNDRAFTED}"


async def sendable_lead_ids(campaign_id: int) -> List[int]:
    """Every lead in the campaign that may still be emailed, drafted or not (for a redraft)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute(f"SELECT id FROM leads WHERE campaign_id = ? AND {SENDABLE} ORDER BY id", (campaign_id,))
        return [r["id"] for r in await cur.fetchall()]
    finally:
        await conn.close()


async def _load(lead_id: int, overwrite: bool = False):
    """The lead and its campaign's value proposition, if a draft request may touch it."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            f"""SELECT l.*, (SELECT value_proposition FROM campaigns c WHERE c.id = l.campaign_id)
                       AS value_proposition,
                   (SELECT draft_instructions FROM campaigns c WHERE c.id = l.campaign_id)
                       AS draft_instructions
                FROM (SELECT * FROM leads WHERE id = ? AND {_draftable(overwrite)}) l""",
            (lead_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


def _stored_facts(raw: Optional[str]) -> Optional[dict]:
    """A lead's saved website facts, or None if it was found before website checks existed."""
    if not raw:
        return None
    try:
        facts = json.loads(raw)
        return facts if isinstance(facts, dict) else None
    except ValueError:
        return None


async def draft_lead(lead_id: int, overwrite: bool = False) -> str:
    """Drafts one lead. Returns 'drafted', 'skipped' (sent, no email, already being drafted,
    or already drafted unless overwrite) or 'failed'. overwrite=True replaces an existing draft."""
    if lead_id in _IN_FLIGHT:
        return "skipped"
    _IN_FLIGHT[lead_id] = -1  # claim before any await, so an overlapping request skips it
    try:
        lead = await _load(lead_id, overwrite)
        if not lead:
            return "skipped"
        _IN_FLIGHT[lead_id] = lead["campaign_id"]
        summary = lead.get("company_summary") or ""
        facts = _stored_facts(lead.get("site_facts"))
        new_facts: Optional[str] = None
        if (not summary or facts is None) and lead.get("website_url"):
            # Leads found before summaries or website checks existed: read the site now (no tokens).
            intel = await scrape_company_website(lead["website_url"])
            summary = summary or (intel.get("content_summary") or "")[:1000]
            if facts is None:
                facts = analyze_site(intel)
                new_facts = json.dumps(facts)
        draft = await generate_cold_email(
            company_name=lead.get("company_name") or lead.get("company_domain") or "",
            company_domain=lead.get("company_domain") or "",
            contact_name=lead.get("contact_name") or "",
            job_title=lead.get("job_title") or "",
            company_summary=summary,
            value_proposition=lead.get("value_proposition") or "",
            instructions=lead.get("draft_instructions"),
            site_facts=(facts or {}).get("lines"),
        )
        subject, body = (draft or {}).get("subject"), (draft or {}).get("body")
        if not subject or not body:
            return "failed"
        conn = await get_db_connection()
        try:
            # Without overwrite, only fill an empty draft: never replace copy someone edited meanwhile.
            await conn.execute(
                f"UPDATE leads SET personalized_subject = ?, personalized_body = ?, "
                f"company_summary = COALESCE(company_summary, ?), site_facts = COALESCE(site_facts, ?) "
                f"WHERE id = ? AND {_draftable(overwrite)}",
                (subject, body, summary or None, new_facts, lead_id),
            )
            await conn.commit()
        finally:
            await conn.close()
        return "drafted"
    except Exception as exc:
        print(f"[Drafting] lead {lead_id} failed: {type(exc).__name__}: {exc}")
        return "failed"
    finally:
        _IN_FLIGHT.pop(lead_id, None)


async def draft_leads(lead_ids: Iterable[int], overwrite: bool = False) -> Dict[str, int]:
    """Drafts many leads, at most DISCOVERY_CONCURRENCY at a time."""
    sem = asyncio.Semaphore(max(1, settings.discovery_concurrency))

    async def one(lid: int) -> str:
        async with sem:
            return await draft_lead(lid, overwrite)

    results = await asyncio.gather(*(one(int(i)) for i in dict.fromkeys(lead_ids)))
    return {k: results.count(k) for k in ("drafted", "skipped", "failed")}


async def draftable_lead_ids(lead_ids: Iterable[int], overwrite: bool = False) -> List[int]:
    """The given leads a draft request may touch (see _draftable), in the order given."""
    ids = list(dict.fromkeys(int(i) for i in lead_ids))
    if not ids:
        return []
    conn = await get_db_connection()
    try:
        marks = ",".join("?" for _ in ids)
        cur = await conn.execute(
            f"SELECT id FROM leads WHERE id IN ({marks}) AND {_draftable(overwrite)}", ids)
        found = {r["id"] for r in await cur.fetchall()}
        return [i for i in ids if i in found]
    finally:
        await conn.close()
