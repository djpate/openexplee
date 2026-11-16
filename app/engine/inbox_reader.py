"""Reads the sending inboxes over IMAP, read-only, to catch what happens after an email goes out.

- A reply from a lead (or a colleague at the same company domain) marks the lead replied and stops
  follow-ups. The reply text is kept for the mailbox view.
- "Unsubscribe", "remove me" and the like suppress the address for good.
- A bounce notice for a lead's address suppresses it and marks the lead bounced.
- Out-of-office auto-replies are ignored.

Only Google Workspace inboxes are read (imap.gmail.com, with the same app password used for SMTP).
Only mail addressed to the sending domain, plus bounce notices, is searched, and nothing is marked read.
"""
import asyncio
import email
import hashlib
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Set

from app.database import get_db_connection

IMAP_HOSTS = {"google": "imap.gmail.com"}
LOOKBACK_DAYS = 3
MAX_MESSAGES_PER_POLL = 200

# Free mail domains: a reply from one only matches a lead by exact address, never by domain.
FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net", "att.net", "verizon.net",
    "protonmail.com", "proton.me", "gmx.com", "mail.com", "zoho.com", "bellsouth.net", "spectrum.net",
}

_UNSUBSCRIBE = re.compile(
    r"\bunsubscribe\b|\bremove me\b|\btake me off\b|\bopt[- ]?out\b|\bstop (?:emailing|sending|contacting)\b"
    r"|\b(?:do not|don't|dont|please don't) (?:email|contact|e-mail|message)\b", re.I)
_AUTO_SUBJECT = re.compile(r"^\s*(?:automatic reply|auto[- ]?reply|autoreply|out of (?:the )?office|away from)", re.I)
_QUOTE_START = re.compile(
    r"^(?:>|-{2,}\s*Original Message|_{5,}|From:\s.+|Sent from my )",
    re.I)

# Messages already handled this run, per inbox id (saves reprocessing; the messages table is the real record).
_SEEN: Dict[int, Set[bytes]] = {}


def _fetch_messages(host: str, user: str, password: str, domain: str, since_days: int) -> List[bytes]:
    """Raw messages from INBOX (opened read-only) addressed to `domain`, or bounce notices."""
    imap = imaplib.IMAP4_SSL(host, 993, timeout=30)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
        uids: Set[bytes] = set()
        for criteria in (("TO", f'"{domain}"'), ("FROM", '"mailer-daemon"'), ("FROM", '"postmaster"')):
            typ, data = imap.uid("SEARCH", None, "SINCE", since, *criteria)
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        raws = []
        for uid in sorted(uids, key=int)[-MAX_MESSAGES_PER_POLL:]:
            typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: doesn't mark it read
            for part in data or []:
                if isinstance(part, tuple):
                    raws.append(part[1])
        return raws
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _text_of(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>|<blockquote.*", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def _new_text(text: str) -> str:
    """The reply itself, without the quoted original underneath."""
    lines = text.replace("\r\n", "\n").split("\n")
    kept = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # "On <date>, <name> wrote:" (Gmail sometimes wraps it over two lines)
        attribution = stripped.startswith("On ") and (
            "wrote" in stripped or (i + 1 < len(lines) and "wrote" in lines[i + 1]))
        if attribution or _QUOTE_START.match(stripped):
            break
        kept.append(line)
    return "\n".join(kept).strip()[:4000]


def _failed_recipients(msg: EmailMessage) -> List[str]:
    found: List[str] = []
    for addr in str(msg.get("X-Failed-Recipients", "")).split(","):
        if "@" in addr:
            found.append(addr.strip().lower())
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            text = part.get_content() if part.get_content_maintype() == "text" else part.as_string()
        except Exception:
            text = part.as_string()
        for block in re.split(r"\n\s*\n", str(text)):
            rcpt = re.search(r"(?im)^Final-Recipient:\s*rfc822;\s*<?([^>\s]+@[^>\s]+)>?", block)
            action = re.search(r"(?im)^Action:\s*(\w+)", block)
            status = re.search(r"(?im)^Status:\s*([245])\.", block)
            if rcpt and ((action and action.group(1).lower() == "failed") or (status and status.group(1) == "5")):
                found.append(rcpt.group(1).lower())
    return list(dict.fromkeys(found))


def classify(raw: bytes) -> Dict[str, Any]:
    """kind: reply | unsubscribe | auto_reply | bounce, plus sender, subject, text and message_id."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(str(msg.get("From", "")))[1].strip().lower()
    subject = str(msg.get("Subject", "") or "")
    message_id = str(msg.get("Message-ID", "") or "").strip() or "<%s@openexplee.inbound>" % hashlib.sha1(raw).hexdigest()
    base = {"sender": sender, "subject": subject, "message_id": message_id}

    local = sender.split("@")[0]
    is_report = msg.get_content_type() == "multipart/report"
    if local in ("mailer-daemon", "postmaster") or is_report:
        return {**base, "kind": "bounce", "recipients": _failed_recipients(msg), "text": ""}

    auto = str(msg.get("Auto-Submitted", "")).lower()
    if (auto and auto != "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond") \
            or str(msg.get("Precedence", "")).lower() == "auto_reply" or _AUTO_SUBJECT.match(subject):
        return {**base, "kind": "auto_reply", "text": ""}

    text = _new_text(_text_of(msg))
    unsubscribing = _UNSUBSCRIBE.search(text) or _UNSUBSCRIBE.search(re.sub(r"(?i)^\s*(re|fwd?)\s*:\s*", "", subject))
    return {**base, "kind": "unsubscribe" if unsubscribing else "reply", "text": text}


async def _lead_for(conn, sender: str) -> Optional[Dict[str, Any]]:
    """The emailed lead this sender belongs to: same address, else same (non-free-mail) domain."""
    cur = await conn.execute(
        "SELECT * FROM leads WHERE LOWER(email) = ? AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1", (sender,))
    row = await cur.fetchone()
    domain = sender.split("@")[-1]
    if not row and domain and domain not in FREE_MAIL:
        cur = await conn.execute(
            "SELECT * FROM leads WHERE LOWER(email) LIKE ? AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
            (f"%@{domain}",))
        row = await cur.fetchone()
    return dict(row) if row else None


async def _record(conn, lead_id: int, item: Dict[str, Any], intent: str) -> bool:
    """Stores the inbound message once; False if it was already handled."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO messages (lead_id, direction, message_id, subject, body, intent) "
        "VALUES (?, 'inbound', ?, ?, ?, ?)",
        (lead_id, item["message_id"], item["subject"][:300], item.get("text", ""), intent))
    return cur.rowcount > 0


async def _apply(item: Dict[str, Any]) -> Optional[str]:
    """Updates leads for one classified message; returns what was done (or None)."""
    from app.engine.sequencer import _auto_suppress_bounce  # avoid an import cycle at load time

    kind = item["kind"]
    if kind == "auto_reply":
        return None
    conn = await get_db_connection()
    try:
        if kind == "bounce":
            done = None
            for rcpt in item.get("recipients", []):
                cur = await conn.execute(
                    "SELECT id FROM leads WHERE LOWER(email) = ? AND sent_at IS NOT NULL", (rcpt,))
                rows = await cur.fetchall()
                for r in rows:
                    if await _record(conn, r["id"], {**item, "message_id": f"{item['message_id']}#{rcpt}"}, "bounce"):
                        await conn.commit()
                        await _auto_suppress_bounce(rcpt, r["id"], "hard_bounce (bounce notice)")
                        done = "bounce"
            await conn.commit()
            return done

        lead = await _lead_for(conn, item["sender"])
        if not lead or not await _record(conn, lead["id"], item, kind):
            await conn.commit()
            return None
        if kind == "unsubscribe":
            await conn.execute(
                "INSERT OR IGNORE INTO suppression_list (entry_type, value, reason) VALUES ('email', ?, ?)",
                (item["sender"], "unsubscribed by reply"))
            await conn.execute(
                "UPDATE leads SET status = 'unsubscribed', is_hot_lead = 0, next_followup_at = NULL WHERE id = ?",
                (lead["id"],))
        else:
            await conn.execute(
                "UPDATE leads SET status = 'replied', is_hot_lead = 1, next_followup_at = NULL WHERE id = ?",
                (lead["id"],))
            await conn.execute(
                "UPDATE campaigns SET hot_leads_count = "
                "(SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND is_hot_lead = 1) WHERE id = ?",
                (lead["campaign_id"], lead["campaign_id"]))
        await conn.commit()
        return kind
    finally:
        await conn.close()


async def poll_inboxes() -> Dict[str, int]:
    """Reads every active Google inbox once; returns counts of replies, unsubscribes and bounces handled."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            f"SELECT * FROM inboxes WHERE status = 'active' AND provider IN ({','.join('?' * len(IMAP_HOSTS))})",
            tuple(IMAP_HOSTS))
        inboxes = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    counts: Dict[str, int] = {}
    for inbox in inboxes:
        password = inbox.get("smtp_password") or ""
        user = inbox.get("smtp_user") or inbox["email"]
        if not password or password == "none" or user == "none":
            continue
        domain = inbox["email"].split("@")[-1]
        try:
            raws = await asyncio.to_thread(
                _fetch_messages, IMAP_HOSTS[inbox["provider"]], user, password, domain, LOOKBACK_DAYS)
        except Exception as exc:
            print(f"[Inbox] couldn't read {inbox['email']}: {type(exc).__name__}: {exc}")
            continue
        seen = _SEEN.setdefault(inbox["id"], set())
        for raw in raws:
            key = hashlib.sha1(raw).digest()
            if key in seen:
                continue
            try:
                done = await _apply(classify(raw))
            except Exception as exc:
                print(f"[Inbox] couldn't process a message for {inbox['email']}: {type(exc).__name__}: {exc}")
                continue
            seen.add(key)
            if done:
                counts[done] = counts.get(done, 0) + 1
    return counts
