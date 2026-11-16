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


async def test_region_area_without_code_is_kept_for_name_lookup(mp_llm):
    # Replaces R4's drop: a region with no code resolves by name (see test_overture).
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Florida", "kind": "region"}, {"name": "Tampa", "kind": "locality", "region_code": "US-FL"}]})
    p = await build_search_plan("x", CATS)
    assert [(a.name, a.kind, a.region_code) for a in p.areas] == [("Florida", "region", None), ("Tampa", "locality", "US-FL")]
    assert not any("Florida" in m for m in p.messages)


@pytest.mark.parametrize("kind,expected", [
    ("city", "locality"), ("Town", "locality"), ("village", "locality"), ("municipality", "locality"),
    ("neighborhood", "locality"), ("state", "region"), ("Province", "region"), ("county", "county"),
    ("parish", "county"), ("borough", "county"), ("nation", "country"), ("Country", "country"),
])
async def test_kind_synonyms_map_to_enum(mp_llm, kind, expected):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Somewhere", "kind": kind, "region_code": "US-FL", "country_code": "US"}]})
    p = await build_search_plan("x", CATS)
    assert [(a.name, a.kind) for a in p.areas] == [("Somewhere", expected)]


async def test_areas_returned_but_none_usable_raises_naming_them(mp_llm):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Narnia", "kind": "galaxy"}, {"name": "Tampa"}, "Orlando, FL"]})
    with pytest.raises(PlanError, match="Couldn't use these areas from the target") as e:
        await build_search_plan("dentists in Narnia", CATS)
    assert "Narnia" in str(e.value) and "Tampa" in str(e.value) and "Orlando, FL" in str(e.value)


async def test_some_unusable_areas_are_named_in_a_message(mp_llm):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Narnia", "kind": "galaxy"}, {"name": "Tampa", "kind": "city", "region_code": "US-FL"}]})
    p = await build_search_plan("x", CATS)
    assert [a.name for a in p.areas] == ["Tampa"]
    assert any("Couldn't use these areas" in m and "Narnia" in m for m in p.messages)


@pytest.mark.parametrize("areas", [None, [], "missing"])
async def test_null_missing_or_empty_areas_use_default(mp_llm, areas):
    data = {"categories": ["dental_clinic"], "is_local_business": True}
    if areas != "missing":
        data["areas"] = areas
    mp_llm(data)
    p = await build_search_plan("dentists", CATS)
    assert [(a.name, a.kind) for a in p.areas] == [("United States", "country")]


@pytest.mark.parametrize("region_code,country_code,expected", [
    ("FL", "US", ("US-FL", "US")),
    (" fl ", None, ("US-FL", None)),
    ("us-fl", " us ", ("US-FL", "US")),
    ("ENG", "gb", ("GB-ENG", "GB")),
    ("Florida", None, (None, None)),
    ("US-FL", "USA", ("US-FL", None)),
])
async def test_codes_are_normalized(mp_llm, region_code, country_code, expected):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "Tampa", "kind": "locality", "region_code": region_code, "country_code": country_code}]})
    p = await build_search_plan("x", CATS)
    assert (p.areas[0].region_code, p.areas[0].country_code) == expected


async def test_country_area_ignores_region_code(mp_llm):
    mp_llm({"categories": ["dental_clinic"], "is_local_business": True,
            "areas": [{"name": "United States", "kind": "country", "region_code": "US-FL", "country_code": "us"}]})
    p = await build_search_plan("x", CATS)
    assert (p.areas[0].region_code, p.areas[0].country_code) == (None, "US")


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
