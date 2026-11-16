import asyncio
import re
import dns.resolver
from email_validator import validate_email, EmailNotValidError
from typing import Dict, Any, List, Optional
import aiosmtplib

# Name we give mail servers when checking an address (HELO/EHLO).
VERIFY_HELO = "verify.openexplee.local"

# A 5xx reply only means "no such mailbox" when it says so; 5.7.x is a block or policy refusal.
_UNKNOWN_MAILBOX = re.compile(
    r"\b5\.1\.\d+\b|\b5\.4\.1\b|does not exist|doesn't exist|user unknown|unknown user|no such user"
    r"|mailbox unavailable|mailbox not found|recipient not found|invalid recipient|address rejected"
    r"|not a valid mailbox|unrouteable|unknown recipient", re.I)


def _mailbox_unknown(code: int, message: str) -> bool:
    if code not in (550, 551, 553) or re.search(r"\b5\.7\.\d+\b", message or ""):
        return False
    return bool(_UNKNOWN_MAILBOX.search(message or ""))


DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
    "sharklasers.com", "getairmail.com", "dispostable.com", "yopmail.com"
}


def generate_email_permutations(first_name: str, last_name: str, domain: str) -> List[str]:
    """Generates standard B2B email permutations from name and domain."""
    f = re.sub(r"[^a-zA-Z]", "", first_name.lower().strip())
    l = re.sub(r"[^a-zA-Z]", "", last_name.lower().strip())
    d = domain.lower().replace("http://", "").replace("https://", "").replace("www.", "").strip("/ ")

    if not f and not l:
        return [f"contact@{d}", f"hello@{d}", f"info@{d}"]
    if not l:
        return [f"{f}@{d}", f"contact@{d}"]
    if not f:
        return [f"{l}@{d}", f"contact@{d}"]

    return [
        f"{f}.{l}@{d}",
        f"{f}@{d}",
        f"{f[0]}{l}@{d}",
        f"{f}{l}@{d}",
        f"{f[0]}.{l}@{d}",
        f"{l}@{d}",
    ]


async def get_mx_records(domain: str) -> List[str]:
    """Performs async DNS MX record lookup, sorted by preference."""
    loop = asyncio.get_running_loop()
    try:
        def _resolve():
            answers = dns.resolver.resolve(domain, "MX")
            records = sorted(answers, key=lambda r: r.preference)
            return [str(r.exchange).rstrip(".") for r in records]

        return await loop.run_in_executor(None, _resolve)
    except Exception:
        return []


async def verify_email_smtp(email: str, timeout: float = 2.0) -> Dict[str, Any]:
    """
    Validates email address through 4 tiers:
    1. Syntax (RFC standard)
    2. Disposable domain check
    3. DNS MX record check
    4. Non-intrusive SMTP Handshake (EHLO -> MAIL FROM -> RCPT TO -> QUIT)
    """
    result = {
        "email": email,
        "status": "unverified",  # 'verified', 'deliverable', 'catch_all', 'invalid', 'risky'
        "score": 0,
        "mx_host": None,
        "is_disposable": False,
        "details": ""
    }

    # Tier 1: Syntax Validation
    try:
        valid = validate_email(email, check_deliverability=False)
        email = valid.normalized
        domain = email.split("@")[1].lower()
    except EmailNotValidError as e:
        result["status"] = "invalid"
        result["details"] = f"Invalid email format: {str(e)}"
        return result

    # Tier 2: Disposable Check
    if domain in DISPOSABLE_DOMAINS:
        result["status"] = "invalid"
        result["is_disposable"] = True
        result["details"] = "Disposable burner email provider"
        return result

    # Tier 3: DNS MX Check
    mx_hosts = await get_mx_records(domain)
    if not mx_hosts:
        result["status"] = "invalid"
        result["details"] = f"Domain {domain} has no valid MX mail servers."
        return result

    primary_mx = mx_hosts[0]
    result["mx_host"] = primary_mx

    # Tier 4: ask the mail server about the mailbox (MAIL FROM:<> -> RCPT TO -> QUIT), sending nothing.
    try:
        smtp_client = aiosmtplib.SMTP(
            hostname=primary_mx, port=25, timeout=timeout, use_tls=False, start_tls=False,
            local_hostname=VERIFY_HELO,
        )
        await smtp_client.connect()
        try:
            await smtp_client.mail("")  # null sender, as bounce messages use
            try:
                rcpt = await smtp_client.rcpt(email)
            except aiosmtplib.SMTPRecipientRefused as exc:
                if _mailbox_unknown(exc.code, exc.message):
                    result["status"] = "invalid"
                    result["score"] = 0
                    result["details"] = f"Mailbox rejected by server: {exc.code} {exc.message[:120]}"
                else:
                    # Blocked IP, policy or greylisting: the server didn't say whether the mailbox exists.
                    result["status"] = "risky"
                    result["score"] = 40
                    result["details"] = f"Server didn't confirm the mailbox ({exc.code} {exc.message[:120]})."
                return result

            if rcpt.code != 250:
                result["status"] = "deliverable"
                result["score"] = 85
                result["details"] = f"MX answered with code {rcpt.code}"
                return result
            try:
                await smtp_client.rcpt(f"check_catchall_987654@{domain}")
                result["status"] = "catch_all"
                result["score"] = 70
                result["details"] = "Domain accepts all mailboxes (Catch-all)."
            except aiosmtplib.SMTPRecipientRefused:
                result["status"] = "verified"
                result["score"] = 99
                result["details"] = "Mailbox exists and is confirmed deliverable."
        finally:
            try:
                await smtp_client.quit()
            except Exception:
                pass

    except Exception as e:
        # Port 25 blocked, timed out, or refused the check: the domain takes mail, but the mailbox is unconfirmed.
        result["status"] = "risky"
        result["score"] = 40
        detail = f"{e.code} {str(e.message)[:120]}" if isinstance(e, aiosmtplib.SMTPResponseException) else type(e).__name__
        result["details"] = f"Domain has mail servers ({primary_mx}). Mailbox not checked ({detail})."

    return result


async def enrich_person_email(first_name: str, last_name: str, domain: str) -> Dict[str, Any]:
    """
    Replicates Explee's enrich_email tool for $0: generates corporate email
    permutations and checks DNS MX + live SMTP handshake to find the best address.
    """
    candidates = generate_email_permutations(first_name, last_name, domain)
    best_fallback: Optional[Dict[str, Any]] = None

    for candidate in candidates:
        res = await verify_email_smtp(candidate, timeout=5.0)
        if res["status"] == "verified":
            return {**res, "candidates_tested": candidates}
        if res["status"] in ("catch_all", "deliverable") and best_fallback is None:
            best_fallback = {**res, "candidates_tested": candidates}
            # Catch-all accepts any address, so no need to probe remaining patterns
            if res["status"] == "catch_all":
                return best_fallback
        if res["mx_host"] is None:
            # Domain has no MX records at all; stop probing permutations
            return {**res, "candidates_tested": [candidate]}

    return best_fallback or {
        "email": candidates[0] if candidates else None,
        "status": "invalid",
        "score": 0,
        "mx_host": None,
        "is_disposable": False,
        "details": "No deliverable permutation found.",
        "candidates_tested": candidates,
    }

