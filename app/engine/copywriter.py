import random
import re
from typing import Dict, Optional
from app.config import settings
from app.engine.llm import call_llm_json


def resolve_spintax(text: str) -> str:
    """Resolves spintax patterns like {Hi|Hello|Hey} into randomized variants."""
    pattern = re.compile(r"\{([^{}]+)\}")
    while pattern.search(text):
        text = pattern.sub(lambda m: random.choice(m.group(1).split("|")), text)
    return text


def default_draft_instructions() -> str:
    """The writing guidance used when a campaign hasn't set its own. Kept as a function so a
    campaign on the default always gets the current version (and the current booking link)."""
    if settings.booking_url:
        ask = ("End with one easy question they can answer by replying to this email, and mention they can "
               f"also pick a time at {settings.booking_url} if they'd rather talk.")
    else:
        ask = "End with one easy question they can answer by replying to this email. Don't ask for a call or a meeting."
    return "\n".join([
        "- They don't know us, so say who we are in the first two sentences: our name, company, that we're a "
        "local team, and what we do in plain words.",
        "- Give one concrete reason we're writing to them specifically: a single detail from their website, "
        "stated plainly. Don't recite their website back to them or flatter them, and don't guess at problems "
        "with their site or business; we haven't audited it.",
        "- Say what we'd actually do for a business like theirs, in one or two plain sentences.",
        f"- {ask}",
        "- 60 to 90 words. Friendly, plain, peer-to-peer. No buzzwords, no hype, no exclamation marks.",
        '- Subject: 2 to 5 lowercase words specific to them. Never start it with "quick question".',
    ])


async def generate_cold_email(
    company_name: str,
    company_domain: str,
    contact_name: str,
    job_title: str,
    company_summary: str,
    value_proposition: str,
    sender_name: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Dict[str, str]:
    """Drafts a short cold email. `instructions` is a campaign's own writing guidance; blank
    means default_draft_instructions(). The facts, greeting, sign-off and output format are
    fixed so custom guidance can't break parsing."""
    sender_name = (sender_name or settings.sender_name or "").strip()
    sender_company = (settings.sender_company or "").strip()
    # "Founder / Hiring Lead" and similar are placeholders from discovery, not a person's name.
    real_contact = bool(contact_name) and "/" not in contact_name
    contact_first = contact_name.split()[0] if real_contact else ""
    greeting = f"Hi {contact_first}," if contact_first else "Hi there,"
    sign_off = "\n".join(x for x in (sender_name, sender_company) if x) or "Best,"
    who = " from ".join(x for x in (sender_name, sender_company) if x) or "the sender (see below)"
    guidance = (instructions or "").strip() or default_draft_instructions()

    prompt = f"""Write a short cold email from {who} to a local business that has never heard of us.

About us (the sender): {who}
{value_proposition}

About them (the recipient):
- Business: {company_name} ({company_domain})
- Contact: {contact_name if real_contact else "unknown"}{f", {job_title}" if real_contact and job_title else ""}
- From their website: {company_summary or "(nothing available)"}

How to write it:
{guidance}

Start with "{greeting}" and end with this sign-off, on its own lines:
{sign_off}

Respond only with JSON: {{"subject": "...", "body": "..."}}"""

    parsed = await call_llm_json(prompt, provider=provider, api_key=api_key)
    if isinstance(parsed, dict) and parsed.get("subject") and parsed.get("body"):
        return {"subject": str(parsed["subject"]), "body": str(parsed["body"])}

    # The AI provider gave nothing usable: a plain template that still says who we are.
    intro = f"I'm {who}" if (sender_name or sender_company) else "I'm reaching out"
    offer = value_proposition.split(". ")[0].rstrip(".") if value_proposition else "we help local businesses"
    ask = "Would it be useful if I sent over a couple of ideas? Just reply to this email."
    if settings.booking_url:
        ask += f" Or pick a time here if you'd rather talk: {settings.booking_url}"
    body = (
        f"{greeting}\n\n"
        f"{intro}, a local team. {offer}.\n\n"
        f"I came across {company_name} and thought it could be a fit.\n\n"
        f"{ask}\n\n"
        f"{sign_off}"
    )
    return {"subject": f"{company_name.lower()} and a local team", "body": body}
