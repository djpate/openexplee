"""Both Claude models (main and name-extraction) are user-settable from the Settings page."""
import httpx
import pytest

from app.api.settings import load_persisted_settings
from app.config import settings
from app.engine import discovery, llm


@pytest.fixture
async def client(db, monkeypatch):
    # Let monkeypatch restore the in-memory settings this test changes through the API.
    monkeypatch.setattr(settings, "anthropic_model", settings.anthropic_model)
    monkeypatch.setattr(settings, "anthropic_extraction_model", settings.anthropic_extraction_model)
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_settings_api_reports_both_claude_models(client):
    data = (await client.get("/api/settings")).json()
    assert data["anthropic_model"] == settings.anthropic_model
    assert data["anthropic_extraction_model"] == settings.anthropic_extraction_model


async def test_saving_models_applies_now_and_survives_restart(client):
    r = await client.post("/api/settings", json={
        "anthropic_model": "claude-sonnet-5-5", "anthropic_extraction_model": "claude-opus-5-5"})
    assert r.status_code == 200
    assert (settings.anthropic_model, settings.anthropic_extraction_model) == ("claude-sonnet-5-5", "claude-opus-5-5")

    settings.anthropic_model, settings.anthropic_extraction_model = "x", "y"
    await load_persisted_settings()
    assert (settings.anthropic_model, settings.anthropic_extraction_model) == ("claude-sonnet-5-5", "claude-opus-5-5")


async def test_name_extraction_follows_the_configured_model(monkeypatch):
    seen = []

    async def fake_call(prompt, api_key, model=None):
        seen.append(model)
        return "[]"

    monkeypatch.setattr(llm, "_call_anthropic", fake_call)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_extraction_model", "claude-sonnet-5-5")
    await discovery._extract_people_with_llm("a.com", "Jo Smith, Owner")
    assert seen == ["claude-sonnet-5-5"]
