"""Safer sending: real spacing, a weekday business-hours window, a working address check,
follow-ups that don't ask for a call, TLS checks for known providers, gentle defaults."""
import asyncio
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosmtplib
import aiosqlite
import httpx
import pytest
from aiosmtplib.response import SMTPResponse

from app.config import settings
from app.engine import scheduler, sequencer, verifier

NY = ZoneInfo("America/New_York")


# ---------- address check (aiosmtplib 5) ----------

@pytest.fixture
def smtp(monkeypatch):
    """A fake mail server: `script['rcpt']` holds (code, message) replies in order."""
    script = {"rcpt": [], "connect_error": None, "calls": []}

    class FakeSMTP:
        def __init__(self, **kw):
            script["calls"].append(("init", kw))

        async def connect(self):
            if script["connect_error"]:
                raise script["connect_error"]

        async def mail(self, sender):
            script["calls"].append(("mail", sender))
            return SMTPResponse(250, "ok")

        async def rcpt(self, recipient):
            code, message = script["rcpt"].pop(0)
            script["calls"].append(("rcpt", recipient))
            if code >= 400:
                raise aiosmtplib.SMTPRecipientRefused(code, message, recipient)
            return SMTPResponse(code, message)

        async def quit(self):
            pass

    async def mx(domain):
        return ["mx.example.com"]

    monkeypatch.setattr(verifier.aiosmtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(verifier, "get_mx_records", mx)
    return script


async def test_existing_mailbox_is_verified_using_a_null_sender(smtp):
    smtp["rcpt"] = [(250, "OK"), (550, "5.1.1 The email account that you tried to reach does not exist.")]
    res = await verifier.verify_email_smtp("info@studio.com")
    assert res["status"] == "verified"
    assert ("mail", "") in smtp["calls"]
    init = smtp["calls"][0][1]
    assert init["start_tls"] is False and init["local_hostname"]


async def test_domain_accepting_everything_is_catch_all(smtp):
    smtp["rcpt"] = [(250, "OK"), (250, "OK")]
    assert (await verifier.verify_email_smtp("info@studio.com"))["status"] == "catch_all"


@pytest.mark.parametrize("message", [
    "5.1.1 The email account that you tried to reach does not exist.",
    "5.4.1 Recipient address rejected: Access denied.",
    "User unknown",
])
async def test_unknown_mailbox_is_invalid(smtp, message):
    smtp["rcpt"] = [(550, message)]
    assert (await verifier.verify_email_smtp("nobody@studio.com"))["status"] == "invalid"


@pytest.mark.parametrize("code,message", [
    (550, "5.7.1 Service unavailable, Client host [1.2.3.4] blocked using Spamhaus."),
    (554, "5.7.1 Message rejected due to local policy"),
    (450, "4.2.1 Greylisted, please try again later"),
])
async def test_blocked_or_greylisted_check_is_not_called_invalid(smtp, code, message):
    smtp["rcpt"] = [(code, message)]
    res = await verifier.verify_email_smtp("info@studio.com")
    assert res["status"] == "risky"
    assert str(code) in res["details"]


async def test_unreachable_mail_server_is_risky(smtp):
    smtp["connect_error"] = OSError("timed out")
    assert (await verifier.verify_email_smtp("info@studio.com"))["status"] == "risky"


# ---------- sending window ----------

def test_window_is_weekday_business_hours_in_the_configured_timezone(monkeypatch):
    monkeypatch.setattr(settings, "send_window_start_hour", 9)
    monkeypatch.setattr(settings, "send_window_end_hour", 17)
    monkeypatch.setattr(settings, "send_weekdays_only", True)
    monkeypatch.setattr(settings, "send_timezone", "America/New_York")
    monday = datetime(2026, 11, 16, tzinfo=NY)
    assert sequencer.in_send_window(monday.replace(hour=9))
    assert sequencer.in_send_window(monday.replace(hour=16, minute=59))
    assert not sequencer.in_send_window(monday.replace(hour=8, minute=59))
    assert not sequencer.in_send_window(monday.replace(hour=17))
    assert not sequencer.in_send_window(datetime(2026, 11, 21, 11, tzinfo=NY))  # Saturday
    # Other timezones are converted: 15:00 UTC is 10:00 in New York.
    assert sequencer.in_send_window(datetime(2026, 11, 16, 15, tzinfo=ZoneInfo("UTC")))
    assert "9:00" in sequencer.send_window_text() and "Mon" in sequencer.send_window_text()


async def _campaign_with_leads(db, n):
    async with aiosqlite.connect(db) as c:
        cid = (await c.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('C', 'i')")).lastrowid
        for i in range(n):
            await c.execute(
                """INSERT INTO leads (campaign_id, company_name, company_domain, email, verification_status, status,
                       personalized_subject, personalized_body) VALUES (?, ?, ?, ?, 'verified', 'enriched', 's', 'b')""",
                (cid, f"c{i}", f"c{i}.com", f"info@c{i}.com"))
        await c.commit()
        return cid


@pytest.fixture
def outbox(monkeypatch):
    sent, sleeps = [], []

    async def inbox():
        return {"id": 1, "email": "me@x.com"}

    async def send(inbox, to_email, subject, body, lead_id=None):
        sent.append(to_email)
        return {"success": True}

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(sequencer, "get_next_available_inbox", inbox)
    monkeypatch.setattr(sequencer, "send_single_email", send)
    monkeypatch.setattr(sequencer.asyncio, "sleep", sleep)
    monkeypatch.setattr(sequencer.random, "randint", lambda a, b: 150)
    return sent, sleeps


async def test_sends_are_spaced_by_the_full_delay_and_not_after_the_last(db, outbox):
    sent, sleeps = outbox
    cid = await _campaign_with_leads(db, 3)
    res = await sequencer.dispatch_campaign_batch(cid, limit=10)
    assert res["dispatched"] == 3 and len(sent) == 3
    assert sleeps == [150, 150]


async def test_nothing_is_sent_outside_the_window(db, outbox, monkeypatch):
    sent, _ = outbox
    monkeypatch.setattr(sequencer, "in_send_window", lambda now=None: False)
    cid = await _campaign_with_leads(db, 2)
    res = await sequencer.dispatch_campaign_batch(cid, limit=10)
    assert sent == [] and res["dispatched"] == 0
    assert any("sending hours" in e for e in res["errors"])


async def test_batch_stops_when_the_window_closes(db, outbox, monkeypatch):
    sent, _ = outbox
    answers = iter([True, False])
    monkeypatch.setattr(sequencer, "in_send_window", lambda now=None: next(answers))
    cid = await _campaign_with_leads(db, 3)
    res = await sequencer.dispatch_campaign_batch(cid, limit=10)
    assert len(sent) == 1 and any("sending hours" in e for e in res["errors"])


@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_launch_outside_the_window_says_when_sending_happens(db, client, monkeypatch):
    monkeypatch.setattr(sequencer, "in_send_window", lambda now=None: False)
    cid = await _campaign_with_leads(db, 1)
    r = (await client.post(f"/api/campaigns/{cid}/launch")).json()
    assert r["status"] == "outside_window" and r["dispatched"] == 0
    assert "sending hours" in r["message"]


# ---------- follow-ups ----------

async def _due_followup(db, step):
    async with aiosqlite.connect(db) as c:
        await c.execute(
            """INSERT INTO leads (campaign_id, company_name, company_domain, email, contact_name, verification_status,
                   status, personalized_subject, followup_step, next_followup_at)
               VALUES (1, 'A', 'a.com', 'info@a.com', 'Mike Smith', 'verified', 'sent', 'hello', ?, '2000-01-01')""",
            (step,))
        await c.commit()


@pytest.fixture
def followups(monkeypatch):
    bodies = []

    async def inbox():
        return {"id": 1, "email": "me@x.com"}

    async def send(inbox, to_email, subject, body, lead_id=None, in_reply_to=None):
        bodies.append(body)
        return {"success": True}

    monkeypatch.setattr(scheduler, "get_next_available_inbox", inbox)
    monkeypatch.setattr(scheduler, "send_single_email", send)
    return bodies


@pytest.mark.parametrize("booking", ["", "https://cal.example.com/me"])
async def test_first_follow_up_never_asks_for_a_call(db, followups, monkeypatch, booking):
    monkeypatch.setattr(settings, "booking_url", booking)
    await _due_followup(db, 1)
    assert await scheduler.process_due_followups() == 1
    body = followups[0].lower()
    assert "call" not in body and "chat" not in body and "meeting" not in body
    assert "reply" in body
    assert (booking in followups[0]) if booking else ("http" not in body)


async def test_follow_ups_wait_for_the_window(db, followups, monkeypatch):
    monkeypatch.setattr(scheduler, "in_send_window", lambda now=None: False)
    await _due_followup(db, 1)
    assert await scheduler.process_due_followups() == 0 and followups == []


# ---------- TLS and defaults ----------

@pytest.mark.parametrize("provider,checked", [
    ("google", True), ("outlook", True), ("hyvor", False), ("custom", False)])
def test_known_providers_get_certificate_checks(provider, checked):
    ctx = sequencer._smtp_tls_context(provider)
    assert (ctx.verify_mode == ssl.CERT_REQUIRED) is checked
    assert ctx.check_hostname is checked


async def test_new_inboxes_default_to_five_a_day(db, client):
    r = await client.post("/api/inboxes", json={
        "email": "christophe@example.dev", "display_name": "Christophe", "provider": "custom", "smtp_host": "smtp.example.dev",
        "smtp_port": 587, "smtp_user": "u", "smtp_password": "p"})
    assert r.status_code == 200, r.text
    async with aiosqlite.connect(db) as c:
        assert (await (await c.execute("SELECT daily_quota FROM inboxes")).fetchone())[0] == 5


async def test_interrupted_sends_are_released_on_startup(db):
    async with aiosqlite.connect(db) as c:
        await c.execute("INSERT INTO campaigns (name, icp_prompt, status) VALUES ('C', 'i', 'sending')")
        await c.commit()
    assert await sequencer.release_interrupted_sends() == 1
    async with aiosqlite.connect(db) as c:
        assert (await (await c.execute("SELECT status FROM campaigns")).fetchone())[0] == "ready"


async def test_mcp_dispatch_runs_in_the_background(monkeypatch):
    from app.api import mcp
    started = asyncio.Event()

    async def slow_batch(campaign_id, limit=10):
        started.set()
        await asyncio.Event().wait()  # would hang a synchronous caller

    monkeypatch.setattr(mcp, "dispatch_campaign_batch", slow_batch)
    res = await asyncio.wait_for(mcp._execute_tool("dispatch_campaign", {"campaign_id": 3, "limit": 5}), 1)
    assert res["status"] == "started"
    await asyncio.wait_for(started.wait(), 1)
    for task in list(mcp._BACKGROUND):
        task.cancel()
