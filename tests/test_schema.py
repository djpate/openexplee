import sqlite3

import aiosqlite
import pytest


async def test_new_tables_and_columns(db):
    async with aiosqlite.connect(db) as c:
        cols = {r[1] for r in await (await c.execute("PRAGMA table_info(campaign_companies)")).fetchall()}
        assert {"campaign_id", "domain", "tier", "status", "skip_reason", "lead_id", "emails", "phones"} <= cols
        camp = {r[1] for r in await (await c.execute("PRAGMA table_info(campaigns)")).fetchall()}
        assert {"search_plan", "overture_release", "pool_total", "pool_pending", "batch_size", "discovery_message"} <= camp
        leads = {r[1] for r in await (await c.execute("PRAGMA table_info(leads)")).fetchall()}
        assert {"phone", "company_id"} <= leads


async def test_startup_resets_processing_rows(db):
    import app.database as database

    async with aiosqlite.connect(db) as c:
        await c.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('c', 'p')")
        await c.execute(
            "INSERT INTO campaign_companies (campaign_id, domain, status) VALUES (1, 'a.com', 'processing')"
        )
        await c.commit()
    await database.init_db()
    async with aiosqlite.connect(db) as c:
        row = await (await c.execute("SELECT status FROM campaign_companies WHERE domain='a.com'")).fetchone()
    assert row[0] == "pending"


async def test_pool_domain_unique_per_campaign(db):
    async with aiosqlite.connect(db) as c:
        await c.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('c', 'p')")
        await c.execute("INSERT INTO campaign_companies (campaign_id, domain) VALUES (1, 'a.com')")
        with pytest.raises(sqlite3.IntegrityError):
            await c.execute("INSERT INTO campaign_companies (campaign_id, domain) VALUES (1, 'a.com')")


def test_settings_defaults():
    from app.config import Settings

    s = Settings(_env_file=None)
    assert (s.discovery_batch_size, s.discovery_concurrency, s.overture_min_confidence, s.default_search_area) == (
        50, 8, 0.5, "United States")
