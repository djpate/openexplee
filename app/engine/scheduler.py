import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.database import get_db_connection
from app.engine.inbox_reader import poll_inboxes
from app.engine.sequencer import (
    compliance_ready, get_next_available_inbox, in_send_window, run_campaign_send, send_single_email,
    sendable_count,
)


async def reset_daily_inbox_quotas() -> int:
    """Resets sent_today to 0 for inboxes whose last send was prior to today (UTC)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            UPDATE inboxes
            SET sent_today = 0
            WHERE sent_today > 0
              AND (last_sent_at IS NULL OR DATE(last_sent_at) < DATE('now'))
        """)
        await conn.commit()
        return cur.rowcount
    finally:
        await conn.close()


async def process_due_followups(limit: int = 10) -> int:
    """Sends scheduled Day-3 / Day-7 follow-ups for leads that have not replied yet."""
    if not in_send_window() or not compliance_ready():
        return 0
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            SELECT * FROM leads
            WHERE status = 'sent'
              AND is_hot_lead = 0
              AND followup_step IN (1, 2)
              AND next_followup_at IS NOT NULL
              AND next_followup_at <= CURRENT_TIMESTAMP
              AND verification_status != 'invalid'
              AND email IS NOT NULL AND email != ''
            LIMIT ?
        """, (limit,))
        leads = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    sent_count = 0
    for lead in leads:
        inbox = await get_next_available_inbox()
        if not inbox:
            break

        orig_subj = lead.get("personalized_subject") or f"quick question for {lead.get('company_name', 'you')}"
        subj = orig_subj if orig_subj.lower().startswith("re:") else f"Re: {orig_subj}"
        first_name = (lead.get("contact_name") or "there").split()[0]

        if lead.get("followup_step") == 1:
            # Same ask as the first email: a reply by email. The booking link only if one is set.
            link_line = f"\n\nIf it's easier, you can also pick a time here: {settings.booking_url}" if settings.booking_url else ""
            body = (
                f"{{Hi|Hey}} {first_name},\n\n"
                f"Just bumping this in case it got buried. If it sounds useful, reply here and "
                f"I'll send over a couple of ideas.{link_line}"
            )
        else:
            body = (
                f"Hi {first_name},\n\n"
                f"Haven't heard back, so I'll assume now isn't the right time and won't follow up again.\n\n"
                f"Feel free to reply anytime if this becomes a priority."
            )

        # Thread it under the last email we sent this lead.
        conn = await get_db_connection()
        try:
            row = await (await conn.execute(
                "SELECT message_id FROM messages WHERE lead_id = ? AND direction = 'outbound' "
                "AND message_id IS NOT NULL ORDER BY id DESC LIMIT 1", (lead["id"],))).fetchone()
        finally:
            await conn.close()
        res = await send_single_email(inbox, lead["email"], subj, body, lead_id=lead["id"],
                                      in_reply_to=row["message_id"] if row else None)
        if res["success"]:
            sent_count += 1

    return sent_count


# Campaigns sending or finding leads because of autopilot, so a tick never starts a second task for one.
_AUTO_TASKS: dict = {}


async def auto_send_tick() -> list:
    """Starts a background batch for each auto-send campaign that has qualified leads left, during
    sending hours, while an inbox has daily quota. Returns the campaign ids started."""
    if not in_send_window() or not compliance_ready():
        return []
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "SELECT id FROM campaigns WHERE auto_send = 1 AND COALESCE(status, '') NOT IN ('sending', 'discovering')")
        ids = [r["id"] for r in await cur.fetchall()]
    finally:
        await conn.close()
    started = []
    for cid in ids:
        task = _AUTO_TASKS.get(cid)
        if task and not task.done():
            continue
        if not await get_next_available_inbox():
            break
        count = await sendable_count(cid)
        if not count:
            continue
        conn = await get_db_connection()
        try:
            await conn.execute("UPDATE campaigns SET status = 'sending' WHERE id = ?", (cid,))
            await conn.commit()
        finally:
            await conn.close()
        _AUTO_TASKS[cid] = asyncio.create_task(run_campaign_send(cid, count))
        started.append(cid)
    return started


# Autopilot finds more leads when fewer than this many days of sending are waiting...
TOPUP_DAYS = 2
# ...running at most this many discovery batches per campaign per day (caps the cost when a
# batch turns up few qualified leads).
MAX_TOPUP_BATCHES_PER_DAY = 3

POOL_CHECKED_MESSAGE = ("Autopilot: every business this search found has been checked. "
                        "Click Refresh search to look again, or widen the campaign to find more.")


def _today() -> str:
    """Today's date where sending happens, so the daily batch limit resets with the sending day."""
    return datetime.now(ZoneInfo(settings.send_timezone)).date().isoformat()


async def daily_send_capacity() -> int:
    """How many emails all active inboxes can send in a day."""
    conn = await get_db_connection()
    try:
        row = await (await conn.execute(
            "SELECT COALESCE(SUM(daily_quota), 0) AS c FROM inboxes WHERE status = 'active'")).fetchone()
        return row["c"] if row else 0
    finally:
        await conn.close()


async def autopilot_topup_tick() -> list:
    """Starts a discovery batch for each auto-send campaign that has fewer than TOPUP_DAYS of
    qualified leads waiting, at most MAX_TOPUP_BATCHES_PER_DAY a day. Nothing runs without a
    sending inbox. Returns the campaign ids started."""
    from app.api.campaigns import run_discovery_task  # the API module imports the engine; avoid a cycle

    capacity = await daily_send_capacity()
    if capacity <= 0:
        return []
    today = _today()
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "SELECT id, pool_pending, overture_release, discovery_message, autopilot_day, autopilot_batches "
            "FROM campaigns WHERE auto_send = 1 AND COALESCE(status, '') NOT IN ('sending', 'discovering')")
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    started = []
    for camp in rows:
        cid = camp["id"]
        task = _AUTO_TASKS.get(cid)
        if task and not task.done():
            continue
        if camp["autopilot_day"] == today and (camp["autopilot_batches"] or 0) >= MAX_TOPUP_BATCHES_PER_DAY:
            continue
        if await sendable_count(cid) >= TOPUP_DAYS * capacity:
            continue
        # A completed search with nothing left to check. (Without a completed search,
        # run_discovery_task fetches the businesses first.)
        if camp["overture_release"] and not camp["pool_pending"]:
            if camp["discovery_message"] != POOL_CHECKED_MESSAGE:
                conn = await get_db_connection()
                try:
                    await conn.execute("UPDATE campaigns SET discovery_message = ? WHERE id = ?",
                                       (POOL_CHECKED_MESSAGE, cid))
                    await conn.commit()
                finally:
                    await conn.close()
            continue
        conn = await get_db_connection()
        try:
            # Claim it (unless someone clicked Find leads or Launch meanwhile) and count today's batch.
            cur = await conn.execute(
                "UPDATE campaigns SET status = 'discovering', autopilot_day = ?, "
                "autopilot_batches = CASE WHEN autopilot_day = ? THEN COALESCE(autopilot_batches, 0) + 1 ELSE 1 END "
                "WHERE id = ? AND auto_send = 1 AND COALESCE(status, '') NOT IN ('sending', 'discovering')",
                (today, today, cid))
            await conn.commit()
        finally:
            await conn.close()
        if not cur.rowcount:
            continue
        _AUTO_TASKS[cid] = asyncio.create_task(run_discovery_task(cid))
        started.append(cid)
    return started


async def scheduler_tick() -> None:
    """One pass: reset quotas, read inboxes (so fresh replies stop follow-ups), follow-ups, auto-send,
    then find more leads for autopilot campaigns running low (after sending, so today's sends go first)."""
    await reset_daily_inbox_quotas()
    await poll_inboxes()
    # One per tick (every 5 minutes), so follow-ups are spaced out too.
    await process_due_followups(limit=1)
    await auto_send_tick()
    await autopilot_topup_tick()


async def run_scheduler_loop(interval_seconds: int = 300) -> None:
    """Background loop: quota resets, inbox reading, follow-ups and automatic sending."""
    while True:
        try:
            await scheduler_tick()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[Scheduler] Background tick error: {exc}")
        await asyncio.sleep(interval_seconds)
