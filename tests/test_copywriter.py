"""The cold-email prompt says who we are, doesn't recite or guess about the prospect, and ends on one clear ask."""
import httpx
import pytest

from app.config import settings
from app.engine import copywriter

LEAD = dict(company_name="Lights Out Boxing", company_domain="lightsoutboxing.com",
            contact_name="Mike Rivera", job_title="Owner",
            company_summary="Boxing gym in Wesley Chapel with classes for all levels.",
            value_proposition="NapoleonLabs is a St. Petersburg team that builds websites and booking tools.")


@pytest.fixture
def sender(monkeypatch):
    monkeypatch.setattr(settings, "sender_name", "Christophe")
    monkeypatch.setattr(settings, "sender_company", "NapoleonLabs")
    monkeypatch.setattr(settings, "booking_url", "")


@pytest.fixture
def captured(monkeypatch):
    prompts = []

    async def fake(prompt, provider=None, api_key=None, model=None):
        prompts.append(prompt)
        return {"subject": "s", "body": "b"}

    monkeypatch.setattr(copywriter, "call_llm_json", fake)
    return prompts


async def test_prompt_introduces_the_sender_and_company(sender, captured):
    await copywriter.generate_cold_email(**LEAD)
    p = captured[0]
    assert "Christophe" in p and "NapoleonLabs" in p
    assert "first two sentences" in p  # who we are, early
    assert "Christophe\nNapoleonLabs" in p  # sign-off


async def test_prompt_forbids_reciting_and_guessing_about_the_prospect(sender, captured):
    await copywriter.generate_cold_email(**LEAD)
    p = captured[0].lower()
    assert "one concrete reason" in p
    assert "don't recite" in p and "don't guess" in p
    # The old prompt's scripted sentences and persona are gone.
    assert "elite" not in p and "hyper-personalized" not in p and "sentence 1" not in p
    assert 'e.g. "quick question' not in p


async def test_reply_ask_when_no_booking_link(sender, captured):
    await copywriter.generate_cold_email(**LEAD)
    p = captured[0]
    assert "reply" in p.lower() and "http" not in p


async def test_booking_link_ask_when_configured(sender, captured, monkeypatch):
    monkeypatch.setattr(settings, "booking_url", "https://cal.com/christophe/15min")
    await copywriter.generate_cold_email(**LEAD)
    assert "https://cal.com/christophe/15min" in captured[0]


async def test_unknown_contact_greets_generically(sender, captured):
    await copywriter.generate_cold_email(**{**LEAD, "contact_name": ""})
    assert "Hi there," in captured[0]


async def test_fallback_template_introduces_sender_and_asks_for_reply(sender, monkeypatch):
    async def none(*a, **k):
        return None

    monkeypatch.setattr(copywriter, "call_llm_json", none)
    out = await copywriter.generate_cold_email(**LEAD)
    assert "Christophe" in out["body"] and "NapoleonLabs" in out["body"]
    assert "reply" in out["body"].lower()
    assert not out["subject"].startswith("quick question")


# Sender company is a setting like sender name.
@pytest.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(settings, "sender_company", settings.sender_company)
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_sender_company_is_a_saved_setting(client):
    r = await client.post("/api/settings", json={"sender_company": "NapoleonLabs"})
    assert r.status_code == 200 and settings.sender_company == "NapoleonLabs"
    assert (await client.get("/api/settings")).json()["sender_company"] == "NapoleonLabs"


# ---------- reply-only ask by default; per-campaign instructions ----------

async def test_default_ask_is_an_email_reply_not_a_call(sender, captured):
    await copywriter.generate_cold_email(**LEAD)
    p = captured[0]
    assert "Don't ask for a call or a meeting" in p
    assert "open to a short call" not in p.lower()


def test_default_instructions_mention_booking_link_only_when_set(monkeypatch):
    monkeypatch.setattr(settings, "booking_url", "")
    assert "http" not in copywriter.default_draft_instructions()
    monkeypatch.setattr(settings, "booking_url", "https://cal.com/x")
    assert "https://cal.com/x" in copywriter.default_draft_instructions()


async def test_custom_instructions_replace_the_default_but_keep_the_fixed_parts(sender, captured):
    await copywriter.generate_cold_email(**LEAD, instructions="Write like a friendly neighbour. Mention parking.")
    p = captured[0]
    assert "Write like a friendly neighbour. Mention parking." in p
    assert "one concrete reason" not in p  # the default guidance is replaced
    # Fixed parts the app relies on stay.
    assert "Christophe" in p and "Lights Out Boxing" in p and "Hi Mike," in p
    assert "Christophe\nNapoleonLabs" in p and '{"subject": "...", "body": "..."}' in p


async def test_blank_instructions_fall_back_to_the_default(sender, captured):
    await copywriter.generate_cold_email(**LEAD, instructions="   ")
    assert "one concrete reason" in captured[0]


async def test_fallback_template_never_asks_for_a_call(sender, monkeypatch):
    async def none(*a, **k):
        return None

    monkeypatch.setattr(copywriter, "call_llm_json", none)
    out = await copywriter.generate_cold_email(**LEAD)
    assert "call" not in out["body"].lower() and "reply" in out["body"].lower()
