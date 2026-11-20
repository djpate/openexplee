import aiosqlite
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.config import settings

DB_FILE = Path(settings.database_path)


async def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        # Campaigns Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icp_prompt TEXT NOT NULL,
            target_industry TEXT,
            status TEXT DEFAULT 'draft',
            value_proposition TEXT,
            reply_to_email TEXT,
            cc_email TEXT,
            total_leads INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            hot_leads_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Leads Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
            company_name TEXT,
            company_domain TEXT,
            website_url TEXT,
            contact_name TEXT,
            first_name TEXT,
            last_name TEXT,
            job_title TEXT,
            email TEXT,
            verification_status TEXT DEFAULT 'unverified',
            verification_reason TEXT,
            personalized_subject TEXT,
            personalized_body TEXT,
            status TEXT DEFAULT 'discovered',
            is_hot_lead BOOLEAN DEFAULT 0,
            sent_at TIMESTAMP,
            sent_from_inbox TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Inboxes / Sending Accounts Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS inboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT,
            provider TEXT NOT NULL,
            smtp_host TEXT NOT NULL,
            smtp_port INTEGER NOT NULL DEFAULT 587,
            smtp_user TEXT NOT NULL,
            smtp_password TEXT NOT NULL,
            reply_to TEXT,
            cc_email TEXT,
            use_tls BOOLEAN DEFAULT 1,
            daily_quota INTEGER DEFAULT 35,
            sent_today INTEGER DEFAULT 0,
            last_sent_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            health_score INTEGER DEFAULT 100,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Warmup Log Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS warmup_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id INTEGER REFERENCES inboxes(id) ON DELETE CASCADE,
            action TEXT,
            peer_address TEXT,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # App Settings Key-Value Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        # Suppression / Deduplication List Table (emails & domains never to contact)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS suppression_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT 'email',
            value TEXT UNIQUE NOT NULL,
            reason TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Conversation history: every email in or out, one row each
        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
            direction TEXT NOT NULL,
            message_id TEXT UNIQUE,
            subject TEXT,
            body TEXT,
            intent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Discovery pool: candidate companies per campaign
        await db.execute("""
        CREATE TABLE IF NOT EXISTS campaign_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
            source TEXT NOT NULL DEFAULT 'overture',
            source_id TEXT,
            name TEXT,
            category TEXT,
            domain TEXT NOT NULL,
            website_url TEXT,
            emails TEXT,
            phones TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            lat REAL,
            lon REAL,
            tier INTEGER DEFAULT 1,
            confidence REAL,
            status TEXT DEFAULT 'pending',
            skip_reason TEXT,
            lead_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(campaign_id, domain)
        );
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cc_campaign_status_tier ON campaign_companies(campaign_id, status, tier)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cc_domain ON campaign_companies(domain)")

        # Safe Schema Migrations for existing DBs
        for table, col, col_type in [
            ("campaigns", "reply_to_email", "TEXT"),
            ("campaigns", "cc_email", "TEXT"),
            ("campaigns", "hot_leads_count", "INTEGER DEFAULT 0"),
            ("leads", "is_hot_lead", "BOOLEAN DEFAULT 0"),
            ("leads", "followup_step", "INTEGER DEFAULT 0"),
            ("leads", "next_followup_at", "TIMESTAMP"),
            ("leads", "reply_intent", "TEXT"),
            ("leads", "auto_replies_sent", "INTEGER DEFAULT 0"),
            ("inboxes", "reply_to", "TEXT"),
            ("inboxes", "cc_email", "TEXT"),
            ("campaigns", "search_plan", "TEXT"),
            ("campaigns", "overture_release", "TEXT"),
            ("campaigns", "pool_total", "INTEGER DEFAULT 0"),
            ("campaigns", "pool_pending", "INTEGER DEFAULT 0"),
            ("campaigns", "batch_size", "INTEGER DEFAULT 50"),
            ("campaigns", "discovery_message", "TEXT"),
            ("leads", "phone", "TEXT"),
            ("leads", "company_id", "INTEGER"),
            ("leads", "company_summary", "TEXT"),
            ("campaigns", "draft_instructions", "TEXT"),
            ("leads", "site_facts", "TEXT"),
            ("leads", "fit_score", "INTEGER"),
            ("leads", "fit_reason", "TEXT"),
            ("leads", "fit_override", "TEXT"),
            ("campaigns", "min_fit_score", "INTEGER DEFAULT 3"),
            ("campaigns", "auto_send", "INTEGER DEFAULT 0"),
            ("campaigns", "autopilot_day", "TEXT"),
            ("campaigns", "autopilot_batches", "INTEGER DEFAULT 0"),
            # How well a found business matches the campaign's target: 2 yes, 1 maybe, 0 no; NULL unscored.
            ("campaign_companies", "target_match", "INTEGER"),
        ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")
            except Exception:
                pass

        # A discovery task dies with the server. On start, nothing can still be discovering.
        await db.execute(
            "UPDATE campaigns SET status = 'error', "
            "discovery_message = 'Interrupted by a restart. Click Find next leads to resume.' "
            "WHERE status = 'discovering'")
        await db.execute("UPDATE campaign_companies SET status = 'pending' WHERE status = 'processing'")
        await db.commit()


async def get_db_connection() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_FILE)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout = 10000;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn

