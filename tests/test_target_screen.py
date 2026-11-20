"""Found businesses are scored against the campaign's target from their map listing, so batches take
the best matches first and clear non-matches are set aside without spending a website check on them."""
import json
import re

import pytest

from app.config import settings
from app.database import get_db_connection
from app.engine import target_screen
from app.engine.company_pool import save_pool
from app.engine.lead_builder import process_next_batch
from tests.test_lead_builder import Fakes, _campaign, _fetch, _rows

TARGET = "Independent escape rooms and arcades in Tampa Bay"


async def _pool(cid, rows):
    """rows: (domain, name, category, confidence)"""
    await save_pool(cid, [{"domain": d, "website_url": f"https://{d}", "name": n, "category": cat, "city": "Tampa",
                           "emails": [], "phones": [], "tier": 1, "confidence": conf}
                          for d, n, cat, conf in rows], "overture")


_SCORE_BY_NAME = object()


class FakeScreen:
    """Stands in for the bulk model: scores each listed business by its name, or gives a fixed reply."""

    def __init__(self, monkeypatch, scores=None, reply=_SCORE_BY_NAME):
        self.scores = scores or {}  # business name -> score
        self.reply = reply
        self.calls = []
        monkeypatch.setattr(target_screen, "call_llm_json", self)

    async def __call__(self, prompt, provider=None, api_key=None, model=None):
        self.calls.append({"prompt": prompt, "model": model})
        if self.reply is not _SCORE_BY_NAME:
            return self.reply
        out = {}
        for line in prompt.splitlines():
            m = re.match(r"^(\d+) \| (.+?) \| ", line)
            if m and m.group(2) in self.scores:
                out[m.group(1)] = self.scores[m.group(2)]
        return out


async def _match(cid):
    rows = await _fetch("SELECT domain, target_match FROM campaign_companies WHERE campaign_id = ?", cid)
    return {r["domain"]: r["target_match"] for r in rows}


async def test_batches_take_the_best_matches_first_and_set_clear_non_matches_aside(db, monkeypatch):
    f = Fakes(monkeypatch)
    FakeScreen(monkeypatch, {"Gold's Gym": 0, "The Vault": 1, "Escape Room Extreme": 2})
    cid = await _campaign()
    # Confidence alone would pick the gym first.
    await _pool(cid, [("gym.com", "Gold's Gym", "gym", 0.99), ("vault.com", "The Vault", "amusement_attraction", 0.9),
                      ("erx.com", "Escape Room Extreme", "gaming_venue", 0.6)])

    out = await process_next_batch(cid, 1, "vp", TARGET)

    assert out["leads"] == 1 and f.scraped == ["erx.com"]
    assert await _rows(cid) == {"erx.com": ("done", None), "vault.com": ("pending", None),
                                "gym.com": ("skipped", "off_target")}
    assert await _match(cid) == {"erx.com": 2, "vault.com": 1, "gym.com": 0}


async def test_the_bulk_model_gets_the_target_and_each_listing(db, monkeypatch):
    Fakes(monkeypatch)
    screen = FakeScreen(monkeypatch)
    cid = await _campaign()
    await _pool(cid, [("erx.com", "Escape Room Extreme", "gaming_venue", 0.6)])
    await process_next_batch(cid, 1, "vp", TARGET)
    assert len(screen.calls) == 1 and screen.calls[0]["model"] == settings.anthropic_extraction_model
    p = screen.calls[0]["prompt"]
    assert TARGET in p and "Escape Room Extreme | gaming_venue | Tampa" in p


async def test_a_business_the_model_did_not_score_stays_in_reserve_as_a_maybe(db, monkeypatch):
    f = Fakes(monkeypatch)
    FakeScreen(monkeypatch, {"Escape Room Extreme": 2, "Gold's Gym": 0})  # The Vault comes back unscored
    cid = await _campaign()
    await _pool(cid, [("vault.com", "The Vault", "amusement_attraction", 0.9),
                      ("erx.com", "Escape Room Extreme", "gaming_venue", 0.6),
                      ("gym.com", "Gold's Gym", "gym", 0.99)])
    await process_next_batch(cid, 2, "vp", TARGET)
    assert sorted(f.scraped) == ["erx.com", "vault.com"]
    assert await _match(cid) == {"erx.com": 2, "vault.com": None, "gym.com": 0}


@pytest.mark.parametrize("reply", [None, "nope", ["2"], {"x": 2}, {"1": "high"}, {"1": 7}, {"1": -1}, {"999": 2}])
async def test_malformed_replies_score_nothing_and_the_batch_still_runs(db, monkeypatch, reply):
    f = Fakes(monkeypatch)
    FakeScreen(monkeypatch, reply=reply)
    cid = await _campaign()
    await _pool(cid, [("erx.com", "Escape Room Extreme", "gaming_venue", 0.6)])
    out = await process_next_batch(cid, 1, "vp", TARGET)
    assert out["leads"] == 1 and f.scraped == ["erx.com"]
    assert await _match(cid) == {"erx.com": None}


async def test_a_large_pool_is_scored_in_chunks(db, monkeypatch):
    Fakes(monkeypatch)
    names = [f"Escape {i}" for i in range(5)]
    screen = FakeScreen(monkeypatch, {n: 2 for n in names})
    monkeypatch.setattr(target_screen, "CHUNK", 2)
    cid = await _campaign()
    await _pool(cid, [(f"e{i}.com", n, "gaming_venue", 0.5) for i, n in enumerate(names)])
    await process_next_batch(cid, 1, "vp", TARGET)
    assert len(screen.calls) == 3
    assert set((await _match(cid)).values()) == {2}


async def test_businesses_are_scored_once(db, monkeypatch):
    Fakes(monkeypatch)
    screen = FakeScreen(monkeypatch, {"A": 2, "B": 2})
    cid = await _campaign()
    await _pool(cid, [("a.com", "A", "arcade", 0.5), ("b.com", "B", "arcade", 0.5)])
    await process_next_batch(cid, 1, "vp", TARGET)
    await process_next_batch(cid, 1, "vp", TARGET)
    assert len(screen.calls) == 1


async def test_without_a_target_nothing_is_scored(db, monkeypatch):
    f = Fakes(monkeypatch)
    screen = FakeScreen(monkeypatch, {"A": 0})
    cid = await _campaign()
    await _pool(cid, [("a.com", "A", "arcade", 0.5)])
    await process_next_batch(cid, 1, "vp", "")
    assert screen.calls == [] and f.scraped == ["a.com"]


async def test_set_aside_businesses_are_counted_apart_from_skipped_ones(db):
    import httpx
    from app.main import app
    cid = await _campaign()
    conn = await get_db_connection()
    try:
        for i, (st, why) in enumerate([("skipped", "off_target"), ("skipped", "off_target"), ("skipped", "no_email"),
                                       ("done", None), ("pending", None)]):
            await conn.execute("INSERT INTO campaign_companies (campaign_id, domain, status, skip_reason) "
                               "VALUES (?, ?, ?, ?)", (cid, f"d{i}.com", st, why))
        await conn.commit()
    finally:
        await conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        it = next(i for i in (await c.get("/api/campaigns")).json() if i["id"] == cid)
    assert (it["pool_off_target"], it["pool_skipped"], it["pool_done"]) == (2, 1, 1)


def test_the_campaign_card_says_how_many_were_set_aside(tmp_path):
    from tests.test_ui_static import run_js
    plan = json.dumps({"categories": ["arcade"], "areas": [{"name": "Tampa", "tier": 1}]})
    out = run_js(tmp_path, ["escapeHtml", "renderPlanBlock"], f"""[
      renderPlanBlock({{id: 1, status: 'ready', search_plan: {plan}, pool_total: 100, pool_done: 3,
                       pool_skipped: 2, pool_off_target: 40, pool_pending: 55}}),
      renderPlanBlock({{id: 2, status: 'ready', search_plan: {plan}, pool_total: 10, pool_done: 3,
                       pool_skipped: 2, pool_off_target: 0, pool_pending: 5}})]""")
    assert "40 don't match the target" in out[0]
    assert "match the target" not in out[1]
