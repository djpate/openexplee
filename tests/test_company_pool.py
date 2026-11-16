import json

import pytest

from app.database import get_db_connection
from app.engine.company_pool import (
    clean_candidates, held_domains, normalize_domain, save_pool, suppressed_domains,
)


def p(url, **kw):
    d = {"source_id": "s", "name": "N", "category": "dentist", "website_url": url, "emails": [], "phones": [],
         "brand": None, "confidence": 0.9, "city": "Tampa", "region": "FL", "country": "US",
         "lat": 1.0, "lon": 2.0, "tier": 1}
    d.update(kw)
    return d


def c(domain):
    return {**p(f"https://{domain}"), "domain": domain, "emails": ["a@" + domain], "phones": ["1"]}


@pytest.mark.parametrize("raw,expected", [
    ("https://www.TampaDental.com/contact?x=1", "tampadental.com"),
    ("tampadental.com", "tampadental.com"), ("http://tampadental.com:8080/", "tampadental.com"),
    ("", ""), (None, ""), ("https://facebook.com/tampadental", "facebook.com"), ("not a url", ""),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_clean_drops_brand_platform_chain_franchise_held_suppressed():
    places = [p("ok.com"), p("brand.com", brand="Brand"), p("https://facebook.com/x"), p("https://hub.biz/x"),
              *[p("chain.com", source_id=f"c{i}") for i in range(4)], p("servpro.com"),
              p("held.com"), p("supp.com"), p("")]
    out = clean_candidates(places, held={"held.com"}, suppressed={"supp.com"})
    assert [x["domain"] for x in out] == ["ok.com"]


def test_clean_dedupes_by_domain_keeping_highest_confidence_and_orders_by_tier():
    out = clean_candidates([p("a.com", confidence=0.6, tier=2), p("a.com", confidence=0.9, tier=2),
                            p("b.com", tier=1)], set(), set())
    assert [(x["domain"], x["confidence"]) for x in out] == [("b.com", 0.9), ("a.com", 0.9)]


def test_three_locations_is_not_a_chain():
    out = clean_candidates([p("tri.com", source_id=str(i)) for i in range(3)], set(), set())
    assert [x["domain"] for x in out] == ["tri.com"]


async def _campaign(name):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("INSERT INTO campaigns (name, icp_prompt) VALUES (?, 'x')", (name,))
        await conn.commit()
        return cur.lastrowid
    finally:
        await conn.close()


async def _counters(cid):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT pool_total, pool_pending FROM campaigns WHERE id=?", (cid,))
        r = await cur.fetchone()
        return r["pool_total"], r["pool_pending"]
    finally:
        await conn.close()


async def test_held_domains_covers_other_campaigns_leads_and_pool(db):
    c1, c2 = await _campaign("one"), await _campaign("two")
    conn = await get_db_connection()
    try:
        await conn.execute("INSERT INTO leads (campaign_id, company_domain) VALUES (?, 'x.com')", (c1,))
        await conn.execute("INSERT INTO campaign_companies (campaign_id, domain, status) VALUES (?, 'y.com', 'pending')", (c1,))
        await conn.execute("INSERT INTO campaign_companies (campaign_id, domain, status) VALUES (?, 'z.com', 'skipped')", (c1,))
        await conn.execute("INSERT INTO campaign_companies (campaign_id, domain, status) VALUES (?, 'own.com', 'pending')", (c2,))
        await conn.commit()
    finally:
        await conn.close()
    assert await held_domains(c2) == {"x.com", "y.com"}


async def test_suppressed_domains(db):
    conn = await get_db_connection()
    try:
        await conn.execute("INSERT INTO suppression_list (entry_type, value) VALUES ('domain', '@Bad.com')")
        await conn.commit()
    finally:
        await conn.close()
    assert await suppressed_domains() == {"bad.com"}


async def test_save_pool_ignores_duplicates_and_updates_counters(db):
    cid = await _campaign("one")
    assert await save_pool(cid, [c("a.com"), c("b.com")], "overture") == 2
    assert await save_pool(cid, [c("a.com"), c("c.com")], "overture") == 1
    assert await _counters(cid) == (3, 3)
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT emails FROM campaign_companies WHERE domain='a.com'")
        assert json.loads((await cur.fetchone())["emails"]) == ["a@a.com"]
    finally:
        await conn.close()


async def test_save_pool_large_batch(db):
    cid = await _campaign("big")
    assert await save_pool(cid, [c(f"d{i}.com") for i in range(20000)], "overture") == 20000
    assert await _counters(cid) == (20000, 20000)
