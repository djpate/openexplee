"""Cleaning rules and per-campaign storage for the Overture company pool."""
import json
from collections import Counter
from typing import Optional
from urllib.parse import urlparse

from app.database import get_db_connection

# Social, platform and directory hosts. Entries containing a dot match the host
# or any subdomain of it (so "x.com" does not hit "box.com"); others are substrings.
PLATFORM_HOSTS: tuple[str, ...] = (
    "facebook", "instagram", "linkedin", "yelp", "linktr.ee", "google", "twitter", "x.com",
    "tiktok", "youtube", "wixsite", "godaddysites", "square.site", "business.site", "hub.biz",
)

FRANCHISE_DOMAINS: frozenset = frozenset({
    "servpro.com", "servicemaster.com", "servicemasterclean.com", "stanleysteemer.com", "chemdry.com",
    "mollymaid.com", "merrymaids.com",
    "jiffylube.com", "midas.com", "meineke.com", "aamco.com",
    "greatclips.com", "sportclips.com", "supercuts.com",
    "massageenvy.com", "handandstone.com", "europeanwax.com",
    "anytimefitness.com", "planetfitness.com", "orangetheory.com", "clubpilates.com", "f45training.com",
    "aspendental.com", "heartland.com", "vcahospitals.com", "banfield.com",
    "homeinstead.com", "visitingangels.com", "comfortkeepers.com",
    "statefarm.com", "allstate.com", "farmers.com",
    "hrblock.com", "jacksonhewitt.com", "libertytax.com",
    "remax.com", "kellerwilliams.com", "coldwellbanker.com", "century21.com",
})

CHAIN_DOMAIN_MIN = 4
_CHUNK = 500
_HELD_POOL_STATUSES = ("pending", "processing", "done")


def normalize_domain(url: Optional[str]) -> str:
    """Return the bare lowercase host of a URL, or "" if unusable."""
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw or " " in raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or " " in host:
        return ""
    return host


def _is_platform(domain: str) -> bool:
    for p in PLATFORM_HOSTS:
        if "." in p:
            if domain == p or domain.endswith("." + p):
                return True
        elif p in domain:
            return True
    return False


def _is_franchise(domain: str) -> bool:
    return domain in FRANCHISE_DOMAINS or any(domain.endswith("." + f) for f in FRANCHISE_DOMAINS)


def clean_candidates(places: list[dict], held: set[str], suppressed: set[str]) -> list[dict]:
    """Drop brands, platforms, chains, franchises, held and suppressed domains; dedupe by domain."""
    with_domain = [(normalize_domain(pl.get("website_url")), pl) for pl in places]
    counts = Counter(d for d, _ in with_domain if d)
    best: dict[str, dict] = {}
    for domain, pl in with_domain:
        if not domain or pl.get("brand"):
            continue
        if counts[domain] >= CHAIN_DOMAIN_MIN:
            continue
        if _is_platform(domain) or _is_franchise(domain) or domain in held or domain in suppressed:
            continue
        cur = best.get(domain)
        if cur is None or (pl.get("confidence") or 0) > (cur.get("confidence") or 0):
            best[domain] = {**pl, "domain": domain}
    return sorted(best.values(), key=lambda c: (c.get("tier") or 1, -(c.get("confidence") or 0)))


async def suppressed_domains() -> set[str]:
    """Loads suppressed company domains from SQLite so they are never re-targeted."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT LOWER(value) AS val FROM suppression_list WHERE entry_type = 'domain'")
        rows = await cur.fetchall()
        return {r["val"].lstrip("@") for r in rows}
    except Exception:
        return set()
    finally:
        await conn.close()


# A lead the user deleted keeps its company reserved, so no campaign picks it up again.
DELETED_BY_USER = "deleted_by_user"


async def held_domains(exclude_campaign_id: Optional[int]) -> set[str]:
    """Domains already held by leads, live pool rows, or user-deleted leads in other campaigns."""
    excl = -1 if exclude_campaign_id is None else exclude_campaign_id
    marks = ",".join("?" for _ in _HELD_POOL_STATUSES)
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "SELECT LOWER(company_domain) AS d FROM leads "
            "WHERE company_domain IS NOT NULL AND company_domain != '' "
            "AND (campaign_id IS NULL OR campaign_id != ?)", (excl,))
        out = {r["d"] for r in await cur.fetchall()}
        cur = await conn.execute(
            f"SELECT LOWER(domain) AS d FROM campaign_companies "
            f"WHERE campaign_id != ? AND (status IN ({marks}) OR skip_reason = ?)",
            (excl, *_HELD_POOL_STATUSES, DELETED_BY_USER))
        out |= {r["d"] for r in await cur.fetchall()}
        return out
    finally:
        await conn.close()


async def refresh_pool_counters(campaign_id: int) -> None:
    conn = await get_db_connection()
    try:
        await conn.execute(
            "UPDATE campaigns SET "
            "pool_total = (SELECT COUNT(*) FROM campaign_companies WHERE campaign_id = ?), "
            "pool_pending = (SELECT COUNT(*) FROM campaign_companies WHERE campaign_id = ? AND status = 'pending') "
            "WHERE id = ?", (campaign_id, campaign_id, campaign_id))
        await conn.commit()
    finally:
        await conn.close()


async def save_pool(campaign_id: int, companies: list[dict], source: str) -> int:
    """Insert companies into the campaign pool (duplicates ignored). Returns rows inserted."""
    rows = [(
        campaign_id, source, c.get("source_id"), c.get("name"), c.get("category"), c["domain"],
        c.get("website_url"), json.dumps(c.get("emails") or []), json.dumps(c.get("phones") or []),
        c.get("city"), c.get("region"), c.get("country"), c.get("lat"), c.get("lon"),
        c.get("tier") or 1, c.get("confidence"),
    ) for c in companies]
    inserted = 0
    conn = await get_db_connection()
    try:
        for i in range(0, len(rows), _CHUNK):
            before = conn.total_changes
            await conn.executemany(
                "INSERT OR IGNORE INTO campaign_companies "
                "(campaign_id, source, source_id, name, category, domain, website_url, emails, phones, "
                "city, region, country, lat, lon, tier, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows[i:i + _CHUNK])
            inserted += conn.total_changes - before
        await conn.commit()
    finally:
        await conn.close()
    await refresh_pool_counters(campaign_id)
    return inserted
