"""Lead fit: how much a business likely needs what this campaign offers (1-5 + a reason).

Rated by the cheap bulk model from the campaign's value proposition, the verified website facts
and the site summary. Leads below the campaign's min_fit_score are disqualified (skipped by
drafting and sending) unless someone overrides that by hand.
"""
import asyncio
import json
from typing import Dict, Iterable, List, Optional, Tuple

from app.config import settings
from app.database import get_db_connection
from app.engine.llm import call_llm_json
from app.engine.site_check import describe_site_facts

DEFAULT_MIN_FIT = 3

# Leads being rated right now, by lead id -> campaign id (for progress in the UI).
_IN_FLIGHT: Dict[int, int] = {}


def qualified_sql(alias: str = "leads") -> str:
    """SQL condition: the lead is qualified. A manual override wins; otherwise unrated leads
    count as qualified and rated ones must reach their campaign's min_fit_score."""
    a = alias
    return (
        # NULL-safe throughout, so NOT (...) works too: `fit_override = 'x'` alone is NULL when unset.
        f"(COALESCE({a}.fit_override, '') = 'qualified' OR (COALESCE({a}.fit_override, '') != 'disqualified' AND "
        f"({a}.fit_score IS NULL OR {a}.fit_score >= COALESCE("
        f"(SELECT min_fit_score FROM campaigns fc WHERE fc.id = {a}.campaign_id), {DEFAULT_MIN_FIT}))))"
    )


def rating_count(campaign_id: int) -> int:
    return sum(1 for cid in _IN_FLIGHT.values() if cid == campaign_id)


async def rate_lead_fit(company_name: str, company_domain: str, summary: str,
                        site_facts: Optional[List[str]], value_proposition: str,
                        target: str = "") -> Tuple[Optional[int], str]:
    """Returns (score 1-5, one-sentence reason), or (None, "") if the model gave nothing usable.
    `target` is the campaign's description of who it's for: other kinds of business score 1."""
    facts = "\n".join(f"- {f}" for f in (site_facts or []) if f) or "- (no website facts available)"
    prompt = f"""You're qualifying a prospect for a cold outreach campaign.

Who this campaign is for:
{target or "(not described)"}

What we offer:
{value_proposition or "(not described)"}

The prospect: {company_name} ({company_domain})
What we verified on their website:
{facts}
From their website: {summary or "(nothing available)"}

Rate from 1 to 5 how much this business likely needs what we offer:
5 = a clear gap our offer fills; 4 = likely useful; 3 = unclear or not enough information;
2 = they mostly have this already; 1 = not a fit at all.
If the text above shows they're clearly not the kind of business this campaign is for (a different
type of business, or a chain or franchise location when it's for independent businesses), rate 1
whatever their website shows.
Base the rating only on the facts and text above. If they're thin, use 3: don't guess.
Give the reason in 15 words or fewer: the deciding fact, without caveats.

Respond only with JSON: {{"score": 1, "reason": "..."}}"""
    parsed = await call_llm_json(prompt, model=settings.anthropic_extraction_model)
    if not isinstance(parsed, dict):
        return None, ""
    try:
        score = int(parsed.get("score"))
    except (TypeError, ValueError):
        return None, ""
    if not 1 <= score <= 5:
        return None, ""
    return score, str(parsed.get("reason") or "").strip()[:300]


def _fact_lines(raw: Optional[str]) -> List[str]:
    """Stored website facts in the current wording (lines stored with them may be outdated)."""
    try:
        facts = json.loads(raw) if raw else None
    except ValueError:
        return []
    return describe_site_facts(facts) if isinstance(facts, dict) else []


async def ratable_lead_ids(lead_ids: Iterable[int], overwrite: bool = False) -> List[int]:
    """The given leads that exist and are unrated (or all of them when overwriting), in order."""
    ids = list(dict.fromkeys(int(i) for i in lead_ids))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    cond = "" if overwrite else " AND fit_score IS NULL"
    conn = await get_db_connection()
    try:
        cur = await conn.execute(f"SELECT id FROM leads WHERE id IN ({marks}){cond}", ids)
        found = {r["id"] for r in await cur.fetchall()}
        return [i for i in ids if i in found]
    finally:
        await conn.close()


async def campaign_lead_ids(campaign_id: int, overwrite: bool = False) -> List[int]:
    cond = "" if overwrite else " AND fit_score IS NULL"
    conn = await get_db_connection()
    try:
        cur = await conn.execute(f"SELECT id FROM leads WHERE campaign_id = ?{cond} ORDER BY id", (campaign_id,))
        return [r["id"] for r in await cur.fetchall()]
    finally:
        await conn.close()


async def rate_lead(lead_id: int, overwrite: bool = False) -> str:
    """Rates one lead: 'rated', 'skipped' (already rated, missing, or being rated) or 'failed'."""
    if lead_id in _IN_FLIGHT:
        return "skipped"
    _IN_FLIGHT[lead_id] = -1  # claim before any await
    try:
        conn = await get_db_connection()
        try:
            cur = await conn.execute(
                "SELECT l.*, c.value_proposition, c.icp_prompt FROM leads l "
                "LEFT JOIN campaigns c ON c.id = l.campaign_id "
                f"WHERE l.id = ?{'' if overwrite else ' AND l.fit_score IS NULL'}", (lead_id,))
            row = await cur.fetchone()
        finally:
            await conn.close()
        if not row:
            return "skipped"
        lead = dict(row)
        _IN_FLIGHT[lead_id] = lead["campaign_id"]
        score, reason = await rate_lead_fit(
            lead.get("company_name") or lead.get("company_domain") or "", lead.get("company_domain") or "",
            lead.get("company_summary") or "", _fact_lines(lead.get("site_facts")), lead.get("value_proposition") or "",
            lead.get("icp_prompt") or "")
        if score is None:
            return "failed"
        conn = await get_db_connection()
        try:
            await conn.execute("UPDATE leads SET fit_score = ?, fit_reason = ? WHERE id = ?", (score, reason, lead_id))
            await conn.commit()
        finally:
            await conn.close()
        return "rated"
    except Exception as exc:
        print(f"[Fit] lead {lead_id} failed: {type(exc).__name__}: {exc}")
        return "failed"
    finally:
        _IN_FLIGHT.pop(lead_id, None)


async def rate_leads(lead_ids: Iterable[int], overwrite: bool = False) -> Dict[str, int]:
    """Rates many leads, at most DISCOVERY_CONCURRENCY at a time."""
    sem = asyncio.Semaphore(max(1, settings.discovery_concurrency))

    async def one(lid: int) -> str:
        async with sem:
            return await rate_lead(lid, overwrite)

    results = await asyncio.gather(*(one(int(i)) for i in dict.fromkeys(lead_ids)))
    return {k: results.count(k) for k in ("rated", "skipped", "failed")}
