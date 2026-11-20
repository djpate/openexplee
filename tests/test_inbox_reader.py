"""Reading the sending inbox (IMAP, read-only): replies stop follow-ups, unsubscribes and bounces are
suppressed; follow-ups thread; the unsubscribe header reaches a real mailbox; no send without a postal
address; optional automatic daily sending."""
import asyncio
from email.message import EmailMessage

import aiosqlite
import httpx
import pytest

from app.config import settings
from app.engine import inbox_reader, scheduler, sequencer


# ---------- building raw messages ----------

def _mail(sender, subject, body, to="christophe@napoleonlabs.dev", msgid=None, headers=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, subject
    m["Message-ID"] = msgid or f"<{abs(hash((sender, subject, body)))}@test>"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


def _bounce(recipient, action="failed", status="5.1.1", failed_header=True):
    m = EmailMessage()
    m["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    m["To"] = "christophe@napoleonlabs.dev"
    m["Subject"] = "Delivery Status Notification (Failure)" if action == "failed" else "Delivery Status Notification (Delay)"
    m["Message-ID"] = f"<bounce-{recipient}-{action}@google>"
    if failed_header:
        m["X-Failed-Recipients"] = recipient
    m.set_content(f"Address not found. Your message wasn't delivered to {recipient}.")
    m.make_mixed()
    report = EmailMessage()
    report.set_content(
        f"Reporting-MTA: dns; googlemail.com\n\nFinal-Recipient: rfc822; {recipient}\n"
        f"Action: {action}\nStatus: {status}\n", subtype="plain")
    m.attach(report)
    return m.as_bytes()


QUOTED = ("Sounds interesting, what would it cost?\n\n"
          "On Mon, Nov 16, 2026 at 10:00 AM Christophe <christophe@napoleonlabs.dev> wrote:\n"
          "> I'm Christophe from NapoleonLabs...\n"
          "> Not the right fit? Just reply \"unsubscribe\" and I won't email you again.\n")


# ---------- classification ----------

def test_reply_ignores_the_quoted_original_with_its_unsubscribe_line():
    c = inbox_reader.classify(_mail("Mike <mike@reef.com>", "Re: 12 hour trips", QUOTED))
    assert c["kind"] == "reply" and c["sender"] == "mike@reef.com"
    assert c["text"].startswith("Sounds interesting") and "unsubscribe" not in c["text"]


@pytest.mark.parametrize("subject,body", [
    ("Re: trips", "Please remove me from your list."),
    ("Re: trips", "Unsubscribe"),
    ("unsubscribe", ""),
    ("Re: trips", "Not interested, please don't email me again."),
])
def test_unsubscribe_requests(subject, body):
    assert inbox_reader.classify(_mail("a@b.com", subject, body))["kind"] == "unsubscribe"


@pytest.mark.parametrize("headers,subject", [
    ({"Auto-Submitted": "auto-replied"}, "Re: trips"),
    ({}, "Automatic reply: Re: trips"),
    ({}, "Out of Office: trips"),
])
def test_auto_replies_are_not_replies(headers, subject):
    assert inbox_reader.classify(_mail("a@b.com", subject, "I'm away until Monday.", headers=headers))["kind"] == "auto_reply"


def test_hard_bounce_names_the_failed_recipient():
    c = inbox_reader.classify(_bounce("info@gone.com"))
    assert c["kind"] == "bounce" and c["recipients"] == ["info@gone.com"]
    c = inbox_reader.classify(_bounce("info@gone.com", failed_header=False))
    assert c["recipients"] == ["info@gone.com"]


def test_delayed_delivery_is_not_a_bounce():
    c = inbox_reader.classify(_bounce("info@slow.com", action="delayed", status="4.4.7", failed_header=False))
    assert c["kind"] == "bounce" and c["recipients"] == []


# ---------- applying results to leads ----------

async def _setup(db):
    async with aiosqlite.connect(db) as c:
        await c.execute("""INSERT INTO inboxes (email, display_name, provider, smtp_host, smtp_port, smtp_user,
                               smtp_password, daily_quota, status)
                           VALUES ('christophe@napoleonlabs.dev', 'C', 'google', 'smtp.gmail.com', 587,
                                   'christophe@verbinnen.org', 'app-pass', 5, 'active')""")
        cid = (await c.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('C', 'i')")).lastrowid
        ids = {}
        for email, status in [("mike@reef.com", "sent"), ("owner@studio.com", "sent"), ("info@gone.com", "sent"),
                              ("info@dance.com", "sent"), ("never@emailed.com", "enriched")]:
            ids[email] = (await c.execute(
                """INSERT INTO leads (campaign_id, company_name, company_domain, email, status, sent_at,
                       followup_step, next_followup_at, verification_status)
                   VALUES (?, 'X', ?, ?, ?, ?, 1, datetime('now', '+3 days'), 'verified')""",
                (cid, email.split("@")[1], email, status, None if status == "enriched" else "2026-11-16"))).lastrowid
        await c.commit()
        return cid, ids


async def _row(db, sql, *args):
    async with aiosqlite.connect(db) as c:
        c.row_factory = aiosqlite.Row
        return await (await c.execute(sql, args)).fetchone()


@pytest.fixture
def mailbox(monkeypatch):
    box = {"raws": [], "calls": []}

    def fetch(host, user, password, domain, since_days):
        box["calls"].append((host, user, password, domain))
        return list(box["raws"])

    monkeypatch.setattr(inbox_reader, "_fetch_messages", fetch)
    inbox_reader._SEEN.clear()
    return box


async def test_poll_marks_replies_unsubscribes_and_bounces(db, mailbox):
    cid, ids = await _setup(db)
    mailbox["raws"] = [
        _mail("Mike <mike@reef.com>", "Re: trips", QUOTED),
        _mail("owner@studio.com", "Re: classes", "Please remove me from your list."),
        _bounce("info@gone.com"),
        _mail("Jane <jane@dance.com>", "Re: studio", "I'm the owner, happy to chat by email."),  # colleague
        _mail("friend@gmail.com", "lunch?", "Lunch Friday?"),  # not a lead
        _bounce("someone@personal.com"),  # a bounce of your own mail
    ]
    res = await inbox_reader.poll_inboxes()
    assert mailbox["calls"] == [("imap.gmail.com", "christophe@verbinnen.org", "app-pass", "napoleonlabs.dev")]
    assert res == {"reply": 2, "unsubscribe": 1, "bounce": 1}

    mike = await _row(db, "SELECT * FROM leads WHERE id=?", ids["mike@reef.com"])
    assert (mike["status"], mike["is_hot_lead"], mike["next_followup_at"]) == ("replied", 1, None)
    msg = await _row(db, "SELECT * FROM messages WHERE lead_id=? AND direction='inbound'", ids["mike@reef.com"])
    assert msg["body"].startswith("Sounds interesting") and msg["intent"] == "reply"

    owner = await _row(db, "SELECT * FROM leads WHERE id=?", ids["owner@studio.com"])
    assert (owner["status"], owner["is_hot_lead"], owner["next_followup_at"]) == ("unsubscribed", 0, None)
    assert await _row(db, "SELECT 1 FROM suppression_list WHERE value='owner@studio.com'")

    gone = await _row(db, "SELECT status FROM leads WHERE id=?", ids["info@gone.com"])
    assert gone["status"] == "bounced"
    assert await _row(db, "SELECT 1 FROM suppression_list WHERE value='info@gone.com'")
    assert not await _row(db, "SELECT 1 FROM suppression_list WHERE value='someone@personal.com'")

    dance = await _row(db, "SELECT status FROM leads WHERE id=?", ids["info@dance.com"])
    assert dance["status"] == "replied"
    camp = await _row(db, "SELECT hot_leads_count FROM campaigns WHERE id=?", cid)
    assert camp["hot_leads_count"] == 2


async def test_each_message_is_handled_once(db, mailbox):
    await _setup(db)
    mailbox["raws"] = [_mail("mike@reef.com", "Re: trips", "Yes please")]
    assert (await inbox_reader.poll_inboxes()) == {"reply": 1}
    inbox_reader._SEEN.clear()  # e.g. after a restart
    assert (await inbox_reader.poll_inboxes()) == {}
    async with aiosqlite.connect(db) as c:
        assert (await (await c.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'")).fetchone())[0] == 1


async def test_unreachable_mailbox_doesnt_break_the_poll(db, monkeypatch):
    await _setup(db)

    def broken(*a):
        raise OSError("login failed")

    monkeypatch.setattr(inbox_reader, "_fetch_messages", broken)
    assert await inbox_reader.poll_inboxes() == {}


async def test_only_google_inboxes_with_a_password_are_read(db, mailbox):
    async with aiosqlite.connect(db) as c:
        await c.execute("""INSERT INTO inboxes (email, display_name, provider, smtp_host, smtp_port, smtp_user,
                               smtp_password, daily_quota, status)
                           VALUES ('a@relay.dev', 'A', 'hyvor', 'relay', 587, 'u', 'p', 5, 'active')""")
        await c.commit()
    await inbox_reader.poll_inboxes()
    assert mailbox["calls"] == []


# ---------- follow-ups thread; unsubscribe header; postal address ----------

async def test_follow_up_replies_to_the_original_message(db, monkeypatch):
    sent = []

    async def inbox():
        return {"id": 1, "email": "christophe@napoleonlabs.dev"}

    async def send(inbox, to_email, subject, body, lead_id=None, in_reply_to=None):
        sent.append(in_reply_to)
        return {"success": True}

    monkeypatch.setattr(scheduler, "get_next_available_inbox", inbox)
    monkeypatch.setattr(scheduler, "send_single_email", send)
    async with aiosqlite.connect(db) as c:
        lid = (await c.execute(
            """INSERT INTO leads (campaign_id, company_name, company_domain, email, contact_name, verification_status,
                   status, personalized_subject, followup_step, next_followup_at)
               VALUES (1, 'A', 'a.com', 'info@a.com', 'Mike', 'verified', 'sent', 'hello', 1, '2000-01-01')""")).lastrowid
        await c.execute("INSERT INTO messages (lead_id, direction, message_id) VALUES (?, 'outbound', '<first@napoleonlabs.dev>')", (lid,))
        await c.commit()
    assert await scheduler.process_due_followups() == 1
    assert sent == ["<first@napoleonlabs.dev>"]


async def test_unsubscribe_header_points_at_the_sending_address(monkeypatch):
    captured = []

    class FakeSMTP:
        def __init__(self, **kw):
            pass

        async def connect(self):
            pass

        async def login(self, u, p):
            pass

        async def send_message(self, msg):
            captured.append(msg)

        async def quit(self):
            pass

    monkeypatch.setattr(sequencer.aiosmtplib, "SMTP", FakeSMTP)
    await sequencer._send_via_smtp({"email": "christophe@napoleonlabs.dev", "provider": "google",
                                    "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_user": "u",
                                    "smtp_password": "p"}, "info@a.com", "s", "b")
    assert captured[0]["List-Unsubscribe"] == "<mailto:christophe@napoleonlabs.dev?subject=unsubscribe>"


@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _campaign_with_ready_lead(db, auto_send=0):
    async with aiosqlite.connect(db) as c:
        cid = (await c.execute("INSERT INTO campaigns (name, icp_prompt, auto_send) VALUES ('C', 'i', ?)",
                               (auto_send,))).lastrowid
        await c.execute("""INSERT INTO leads (campaign_id, company_name, company_domain, email, verification_status,
                               status, personalized_subject, personalized_body)
                           VALUES (?, 'A', 'a.com', 'info@a.com', 'verified', 'enriched', 's', 'b')""", (cid,))
        await c.commit()
        return cid


async def test_no_postal_address_no_sending(db, client, monkeypatch):
    monkeypatch.setattr(settings, "postal_address", "  ")
    cid = await _campaign_with_ready_lead(db)
    r = (await client.post(f"/api/campaigns/{cid}/launch")).json()
    assert r["status"] == "no_postal_address" and "postal address" in r["message"]
    res = await sequencer.dispatch_campaign_batch(cid, limit=5)
    assert res["dispatched"] == 0 and any("postal address" in e for e in res["errors"])
    assert await scheduler.process_due_followups() == 0


# ---------- automatic daily sending ----------

@pytest.fixture
def started(monkeypatch):
    runs = []

    async def run(campaign_id, limit):
        runs.append((campaign_id, limit))

    async def inbox():
        return {"id": 1, "email": "me@x.com"}

    monkeypatch.setattr(scheduler, "run_campaign_send", run)
    monkeypatch.setattr(scheduler, "get_next_available_inbox", inbox)
    return runs


async def test_auto_send_starts_enabled_campaigns_only(db, started):
    on = await _campaign_with_ready_lead(db, auto_send=1)
    await _campaign_with_ready_lead(db, auto_send=0)
    assert await scheduler.auto_send_tick() == [on]
    await asyncio.sleep(0)
    assert started == [(on, 1)]
    assert (await _row(db, "SELECT status FROM campaigns WHERE id=?", on))["status"] == "sending"
    assert await scheduler.auto_send_tick() == []  # already sending


async def test_auto_send_waits_for_window_inbox_and_address(db, started, monkeypatch):
    await _campaign_with_ready_lead(db, auto_send=1)
    monkeypatch.setattr(scheduler, "in_send_window", lambda now=None: False)
    assert await scheduler.auto_send_tick() == []
    monkeypatch.setattr(scheduler, "in_send_window", lambda now=None: True)
    monkeypatch.setattr(settings, "postal_address", "")
    assert await scheduler.auto_send_tick() == []
    monkeypatch.setattr(settings, "postal_address", "1 Main St")

    async def no_inbox():
        return None

    monkeypatch.setattr(scheduler, "get_next_available_inbox", no_inbox)
    assert await scheduler.auto_send_tick() == []


async def test_auto_send_toggle_api(db, client):
    cid = await _campaign_with_ready_lead(db)
    assert (await client.put(f"/api/campaigns/{cid}/auto-send", json={"enabled": True})).status_code == 200
    c = [x for x in (await client.get("/api/campaigns")).json() if x["id"] == cid][0]
    assert c["auto_send"] == 1
    assert (await client.put("/api/campaigns/999/auto-send", json={"enabled": True})).status_code == 404


# ---------- replies show in the mailbox ----------

async def test_thread_shows_the_reply(db, client, mailbox):
    _, ids = await _setup(db)
    mailbox["raws"] = [_mail("mike@reef.com", "Re: trips", QUOTED)]
    await inbox_reader.poll_inboxes()
    data = (await client.get(f"/api/mailbox/threads/{ids['mike@reef.com']}")).json()
    inbound = [m for m in data["messages"] if m["direction"] == "inbound"]
    assert inbound and inbound[0]["body"].startswith("Sounds interesting")
    assert inbound[0]["sender"] == "mike@reef.com"


def test_lines_starting_with_on_are_kept():
    c = inbox_reader.classify(_mail("a@b.com", "Re: x", "On Tuesday I'm free to talk.\nThanks\n\n" + QUOTED.split("\n\n", 1)[1]))
    assert c["text"] == "On Tuesday I'm free to talk.\nThanks"


def test_wrapped_gmail_attribution_is_cut():
    body = "Yes!\n\nOn Mon, Nov 16, 2026 at 10:00 AM Christophe <\nchristophe@napoleonlabs.dev> wrote:\n> unsubscribe line"
    c = inbox_reader.classify(_mail("a@b.com", "Re: x", body))
    assert (c["kind"], c["text"]) == ("reply", "Yes!")
