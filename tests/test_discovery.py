import pytest

from app.config import settings
from app.database import get_db_connection
from app.engine import discovery
from app.engine.discovery import refresh_campaign_pool, search_companies_for_icp, search_web_candidates
from app.engine.overture import AreaSpec
from app.engine.search_plan import PlanError, SearchPlan
from tests.overture_fixtures import division, overture_files, place  # noqa: F401


def local_plan(areas=None):
    return SearchPlan(["dental_clinic"], areas or [AreaSpec("Tampa", "locality", 1, "US-FL", "US")], True, [])


@pytest.fixture
def world(overture_files, monkeypatch):
    write_places, write_divisions = overture_files
    calls = {"plan": 0, "serper": [], "bing": [], "ddg": []}
    state = {"plan": local_plan()}

    def set_places(*rows):
        write_places(settings.overture_places_source, list(rows))

    set_places(place())
    write_divisions(settings.overture_divisions_source, [division()])

    async def fake_plan(icp, cats):
        calls["plan"] += 1
        return state["plan"]

    async def fake_serper(q, key, max_results=15):
        calls["serper"].append(q)
        return [{"title": "Acme - Home", "url": "https://acme.io", "domain": "acme.io", "snippet": ""},
                {"title": "Acme", "url": "https://linkedin.com/company/acme", "domain": "linkedin.com", "snippet": ""}]

    async def fake_queries(icp):
        return ["q1", "q2", "q3"]

    async def fake_bing(q, max_results=10):
        calls["bing"].append(q)
        return []

    monkeypatch.setattr(discovery, "build_search_plan", fake_plan)
    monkeypatch.setattr(discovery, "search_serper", fake_serper)
    monkeypatch.setattr(discovery, "_search_queries_for_icp", fake_queries)
    monkeypatch.setattr(discovery, "search_bing", fake_bing)
    monkeypatch.setattr(discovery, "search_duckduckgo", fake_bing)
    monkeypatch.setattr(settings, "serper_api_key", "")
    monkeypatch.setattr(settings, "serper_max_queries", 2)
    return calls, state, set_places


async def test_local_icp_uses_overture_only(world):
    calls, _, _ = world
    plan, cands, msgs, rel = await search_companies_for_icp("dentists in Tampa, FL")
    assert [c["domain"] for c in cands] == ["tampadental.com"] and rel == "local"
    assert calls["serper"] == calls["bing"] == []


async def test_non_local_without_serper_key_warns(world):
    calls, state, _ = world
    state["plan"] = SearchPlan([], [], False, [])
    _, cands, msgs, _ = await search_companies_for_icp("saas")
    assert "Open map data covers it poorly" in " ".join(msgs) and cands == [] and calls["bing"] == []


async def test_non_local_with_serper_key_adds_web_candidates(world, monkeypatch):
    calls, state, _ = world
    monkeypatch.setattr(settings, "serper_api_key", "k")
    state["plan"] = SearchPlan([], [], False, [])
    _, cands, msgs, _ = await search_companies_for_icp("saas")
    assert [c["domain"] for c in cands] == ["acme.io"] and cands[0]["confidence"] == 0.5
    assert len(calls["serper"]) == 2 and calls["bing"] == []
    assert not any("map data" in m for m in msgs)


async def test_non_local_with_categories_still_runs_overture_no_key(world):
    calls, state, _ = world
    state["plan"] = SearchPlan(["dental_clinic"], [AreaSpec("Tampa", "locality", 1, "US-FL", "US")], False, [])
    _, cands, msgs, _ = await search_companies_for_icp("x")
    assert [c["domain"] for c in cands] == ["tampadental.com"]
    assert "Open map data covers it poorly" in " ".join(msgs) and calls["bing"] == []


async def test_non_local_with_categories_and_key_returns_both(world, monkeypatch):
    calls, state, _ = world
    monkeypatch.setattr(settings, "serper_api_key", "k")
    state["plan"] = SearchPlan(["dental_clinic"], [AreaSpec("Tampa", "locality", 1, "US-FL", "US")], False, [])
    _, cands, msgs, _ = await search_companies_for_icp("x")
    assert {c["domain"] for c in cands} == {"tampadental.com", "acme.io"} and calls["bing"] == []
    assert not any("map data" in m for m in msgs)


async def test_search_web_candidates_without_key_is_empty(world):
    assert await search_web_candidates("x") == []


async def _campaign():
    conn = await get_db_connection()
    try:
        cur = await conn.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('c', 'dentists in Tampa')")
        await conn.commit()
        return cur.lastrowid
    finally:
        await conn.close()


async def test_refresh_reuses_stored_plan_and_only_adds_new(db, world):
    calls, _, set_places = world
    cid = await _campaign()
    r1 = await refresh_campaign_pool(cid)
    assert r1["inserted"] == 1 and r1["pool_total"] == 1
    set_places(place(), place(id="p2", websites=["https://newdental.com"], names={"primary": "New"}))
    r2 = await refresh_campaign_pool(cid)
    assert calls["plan"] == 1 and r2["inserted"] == 1 and r2["pool_total"] == 2
    conn = await get_db_connection()
    try:
        n = (await (await conn.execute("SELECT count(*) c FROM leads")).fetchone())["c"]
        row = await (await conn.execute("SELECT overture_release, search_plan FROM campaigns WHERE id=?", (cid,))).fetchone()
    finally:
        await conn.close()
    assert n == 0 and row["overture_release"] == "local" and row["search_plan"]


async def test_refresh_skips_domains_held_by_other_campaigns(db, world):
    other = await _campaign()
    await refresh_campaign_pool(other)
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE campaign_companies SET status='done' WHERE campaign_id=?", (other,))
        await conn.commit()
    finally:
        await conn.close()
    cid = await _campaign()
    r = await refresh_campaign_pool(cid)
    assert r["inserted"] == 0


async def test_unresolved_areas_reported_and_all_unresolved_raises(world):
    _, state, _ = world
    state["plan"] = local_plan([AreaSpec("Tampa", "locality", 1, "US-FL", "US"),
                                AreaSpec("Nowhereville", "locality", 2, "US-FL", "US")])
    _, cands, msgs, _ = await search_companies_for_icp("x")
    assert cands and any("Nowhereville" in m for m in msgs)
    state["plan"] = local_plan([AreaSpec("Nowhereville", "locality", 1, "US-FL", "US")])
    with pytest.raises(PlanError, match="None of the places"):
        await search_companies_for_icp("x")


def test_llm_recall_is_gone():
    assert not hasattr(discovery, "_suggest_domains_via_llm") and not hasattr(discovery, "ICP_CURATED_DOMAINS")
