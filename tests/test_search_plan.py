import pytest

from app.engine import search_plan
from app.engine.overture import AreaSpec
from app.engine.search_plan import (
    PlanError, SearchPlan, build_search_plan, plan_from_json, plan_to_json,
)

CATS = ["dental_clinic", "chiropractic", "hvac_service", "software_development"]


@pytest.fixture
def mp_llm(monkeypatch):
    def set_result(result):
        async def fake(prompt, provider=None, api_key=None):
            return result
        monkeypatch.setattr(search_plan, "call_llm_json", fake)
    return set_result


async def test_unknown_categories_dropped_and_areas_parsed(mp_llm):
    mp_llm({"categories": ["dental_clinic", "Chiropractic", "made_up"], "is_local_business": True,
            "areas": [{"name": "Pinellas County", "kind": "county", "region_code": "US-FL", "tier": 1},
                      {"name": "Florida", "kind": "region", "region_code": "US-FL", "tier": 2}]})
    p = await build_search_plan("dentists and chiropractors, Pinellas first then Florida", CATS)
    assert p.categories == ["dental_clinic", "chiropractic"]
    assert [(a.name, a.kind, a.tier) for a in p.areas] == [("Pinellas County", "county", 1), ("Florida", "region", 2)]


async def test_no_place_uses_default_area_and_says_so(mp_llm):
    mp_llm({"categories": ["software_development"], "is_local_business": False, "areas": []})
    p = await build_search_plan("SaaS startups using Stripe", CATS)
    assert [(a.name, a.kind) for a in p.areas] == [("United States", "country")]
    assert any("United States" in m for m in p.messages)


async def test_no_valid_category_for_local_icp_raises(mp_llm):
    mp_llm({"categories": ["made_up"], "is_local_business": True, "areas": [{"name": "Tampa", "kind": "locality"}]})
    with pytest.raises(PlanError, match="categor"):
        await build_search_plan("x", CATS)


async def test_no_category_ok_for_non_local_icp(mp_llm):
    mp_llm({"categories": [], "is_local_business": False, "areas": [{"name": "Texas", "kind": "region", "region_code": "US-TX"}]})
    p = await build_search_plan("x", CATS)
    assert p.categories == [] and p.is_local_business is False


async def test_llm_returns_nothing_raises_readable_error(mp_llm):
    for bad in (None, "garbage", [], {"areas": "nope"}):
        mp_llm(bad)
        with pytest.raises(PlanError):
            await build_search_plan("x", CATS)


async def test_invalid_kind_dropped_and_missing_tier_defaults(mp_llm):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Narnia", "kind": "galaxy"}, {"name": "Tampa", "kind": "locality", "region_code": "US-FL"}]})
    p = await build_search_plan("x", CATS)
    assert [(a.name, a.tier) for a in p.areas] == [("Tampa", 1)]


async def test_region_area_without_code_is_dropped_with_message(mp_llm):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Florida", "kind": "region"}, {"name": "Tampa", "kind": "locality", "region_code": "US-FL"}]})
    p = await build_search_plan("x", CATS)
    assert [a.name for a in p.areas] == ["Tampa"]
    assert any("Florida" in m for m in p.messages)


async def test_prompt_requires_region_codes_and_lists_categories(monkeypatch):
    seen = {}

    async def fake(prompt, provider=None, api_key=None):
        seen["p"] = prompt
        return {"categories": ["dental_clinic"], "is_local_business": True, "areas": []}
    monkeypatch.setattr(search_plan, "call_llm_json", fake)
    await build_search_plan("x", CATS)
    assert "ISO 3166-2" in seen["p"] and "US-FL" in seen["p"] and "hvac_service" in seen["p"]


PLAN = SearchPlan(["dental_clinic"], [AreaSpec("Florida", "region", 2, "US-FL", "US")], True, ["hi"])


def test_plan_json_roundtrip():
    assert plan_from_json(plan_to_json(PLAN)) == PLAN
