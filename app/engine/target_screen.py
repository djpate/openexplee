"""Score found businesses against the campaign's target from their map listing (name, category, city).

Batches then take the best matches first, and businesses that clearly aren't the kind the campaign is
for are set aside (skip_reason "off_target") before a website check is spent on them. The bulk model
reads the listings in chunks; a business it doesn't score stays a maybe and is tried again next batch.
"""
import asyncio
from typing import Dict, List

from app.config import settings
from app.database import get_db_connection
from app.engine.company_pool import refresh_pool_counters
from app.engine.llm import call_llm_json

CHUNK = 150
OFF_TARGET = "off_target"
_SCORES = {"0": 0, "1": 1, "2": 2}


def _prompt(target: str, rows: List[dict]) -> str:
    listings = "\n".join(
        f"{r['id']} | {r.get('name') or r['domain']} | {r.get('category') or 'unknown'} | {r.get('city') or ''}"
        for r in rows)
    return f"""You're sorting businesses for a cold outreach campaign, using only their map listing.

Who the campaign is for:
{target}

Score each business:
2 = clearly the kind of business the campaign is for
1 = could be; the listing doesn't say enough to tell
0 = clearly a different kind of business, or a national chain or franchise location when the campaign
    is for independent businesses

Businesses, one per line as id | name | category | city:
{listings}

Respond only with JSON mapping each id to its score, like {{"12": 2, "13": 0}}."""


async def score_listings(target: str, rows: List[dict]) -> Dict[int, int]:
    """id -> 0, 1 or 2 for the rows the model scored; anything malformed is left out."""
    parsed = await call_llm_json(_prompt(target, rows), model=settings.anthropic_extraction_model)
    if not isinstance(parsed, dict):
        return {}
    ids = {r["id"] for r in rows}
    out: Dict[int, int] = {}
    for key, value in parsed.items():
        score = _SCORES.get(str(value)) if isinstance(value, (int, str)) and not isinstance(value, bool) else None
        try:
            rid = int(key)
        except (TypeError, ValueError):
            continue
        if rid in ids and score is not None:
            out[rid] = score
    return out


async def screen_pending(campaign_id: int, target: str) -> Dict[str, int]:
    """Scores the campaign's unscored pending businesses; sets aside the clear non-matches."""
    if not (target or "").strip():
        return {"scored": 0, "off_target": 0}
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "SELECT id, name, category, city, domain FROM campaign_companies "
            "WHERE campaign_id = ? AND status = 'pending' AND target_match IS NULL ORDER BY id", (campaign_id,))
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()
    if not rows:
        return {"scored": 0, "off_target": 0}

    sem = asyncio.Semaphore(max(1, settings.discovery_concurrency))

    async def one(chunk: List[dict]) -> Dict[int, int]:
        async with sem:
            try:
                return await score_listings(target, chunk)
            except Exception as exc:  # a failed chunk stays unscored: a maybe
                print(f"[TargetScreen] campaign {campaign_id}: {type(exc).__name__}: {exc}")
                return {}

    results = await asyncio.gather(*(one(rows[i:i + CHUNK]) for i in range(0, len(rows), CHUNK)))
    scores = {rid: s for res in results for rid, s in res.items()}
    conn = await get_db_connection()
    try:
        # Only rows still pending: a batch may have claimed one meanwhile.
        await conn.executemany(
            "UPDATE campaign_companies SET target_match = ?, "
            "status = CASE WHEN ? = 0 AND status = 'pending' THEN 'skipped' ELSE status END, "
            "skip_reason = CASE WHEN ? = 0 AND status = 'pending' THEN ? ELSE skip_reason END "
            "WHERE id = ?", [(s, s, s, OFF_TARGET, rid) for rid, s in scores.items()])
        await conn.commit()
    finally:
        await conn.close()
    await refresh_pool_counters(campaign_id)
    return {"scored": len(scores), "off_target": sum(1 for s in scores.values() if s == 0)}
