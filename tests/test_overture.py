import pytest

from app.engine import overture
from app.engine.overture import AreaSpec, ResolvedArea
from tests.overture_fixtures import division, place
from tests.overture_fixtures import overture_files  # noqa: F401

FL_BOX = (-87.63, 24.4, -80.03, 31.0)
GA_BOX = (-85.6, 30.36, -80.8, 35.0)
TAMPA_BOX = (-82.6, 27.85, -82.25, 28.17)


def area(name="Tampa", tier=1, box=TAMPA_BOX, region_values=None, country_code=None, kind="locality"):
    return ResolvedArea(name, kind, tier, [box], region_values, country_code)


def test_get_release_local_when_sources_overridden(overture_files):
    assert overture.get_release() == "local"


def test_get_release_uses_setting(overture_files, monkeypatch):
    monkeypatch.setattr(overture.settings, "overture_places_source", "")
    monkeypatch.setattr(overture.settings, "overture_release", "2026-10-21.0")
    assert overture.get_release() == "2026-10-21.0"


def test_load_categories_distinct_sorted_and_cached(overture_files, monkeypatch):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [
        place(id="a", category="dental_clinic"), place(id="b", category="hvac_service"),
        place(id="c", category="dental_clinic"), place(id="d", basic_category=None),
    ])
    assert overture.load_categories("local") == ["dental_clinic", "hvac_service"]
    monkeypatch.setattr(overture.duckdb, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not cached")))
    assert overture.load_categories("local") == ["dental_clinic", "hvac_service"]


def test_resolve_merges_duplicate_rows_into_union_box(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="1", name="Pinellas County", subtype="county", box=(-82.909, 27.610, -82.541, 28.173)),
        division(id="2", name="Pinellas County", subtype="county", box=(-82.852, 27.611, -82.583, 28.173)),
    ])
    areas, missing = overture.resolve_areas([AreaSpec("Pinellas County", "county", 1, "US-FL")], "local")
    assert missing == [] and areas[0].boxes == [(-82.909, 27.610, -82.541, 28.173)]


def test_resolve_disambiguates_by_region_code(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="1", name="Hillsborough County", subtype="county", box=(-82.8, 27.6, -82.0, 28.2)),
        division(id="2", name="Hillsborough County", subtype="county", region="US-NH", box=(-72.0, 42.7, -71.3, 43.1)),
    ])
    areas, _ = overture.resolve_areas([AreaSpec("Hillsborough County", "county", 1, "US-FL")], "local")
    assert areas[0].boxes == [(-82.8, 27.6, -82.0, 28.2)]


def test_region_without_code_resolves_by_name_and_division_region_column(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="fl", name="Florida", subtype="region", box=FL_BOX),
        division(id="fl-city", name="Florida", subtype="locality", region="US-NY", box=(-74.4, 41.3, -74.3, 41.4)),
        division(id="x", name="Florida", subtype="region", country="XX", region="XX-FL", box=(1, 1, 2, 2)),
    ])
    areas, missing = overture.resolve_areas([AreaSpec("Florida", "region", 1, None, "US")], "local")
    assert missing == [] and areas[0].boxes == [FL_BOX]
    assert areas[0].region_values == {"FL", "Florida"} and areas[0].country_code == "US"
    areas, missing = overture.resolve_areas([AreaSpec("florida", "region", 1)], "local")
    assert areas == [] and missing == ["florida"]  # FL in US and XX: ambiguous without a country


@pytest.mark.parametrize("name,kind,regions", [
    ("Hillsborough County", "county", ("US-FL", "US-NH")),
    ("Portland", "locality", ("US-ME", "US-OR")),
])
def test_ambiguous_area_is_reported_not_unioned(overture_files, name, kind, regions):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="a", name=name, subtype=kind, region=regions[1], box=(-123.0, 45.4, -122.4, 45.7)),
        division(id="b", name=name, subtype=kind, region=regions[0], box=(-70.4, 43.6, -70.2, 43.7)),
    ])
    msgs = []
    areas, missing = overture.resolve_areas([AreaSpec(name, kind, 1)], "local", msgs)
    assert areas == [] and missing == [name]
    assert f"{name} is ambiguous ({regions[0]}, {regions[1]}). Add the state." in msgs
    areas, missing = overture.resolve_areas([AreaSpec(name, kind, 1, regions[0])], "local")
    assert missing == [] and areas[0].boxes == [(-70.4, 43.6, -70.2, 43.7)]


def test_same_region_rows_without_code_still_union(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="1", name="Pinellas County", subtype="county", box=(-82.909, 27.610, -82.541, 28.173)),
        division(id="2", name="Pinellas County", subtype="county", box=(-82.852, 27.611, -82.583, 28.173)),
    ])
    msgs = []
    areas, missing = overture.resolve_areas([AreaSpec("Pinellas County", "county", 1)], "local", msgs)
    assert missing == [] and msgs == [] and areas[0].boxes == [(-82.909, 27.610, -82.541, 28.173)]


def test_resolve_reports_not_found_names_in_messages(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [division()])
    msgs = []
    overture.resolve_areas([AreaSpec("Gotham", "locality", 1, "US-FL"), AreaSpec("Tampa", "locality", 1)],
                           "local", msgs)
    assert msgs == ["Couldn't find these places in the map data: Gotham"]


def test_resolve_retries_saint_st_variant(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source,
                    [division(name="Saint Petersburg", box=(-82.8, 27.65, -82.6, 27.9))])
    areas, missing = overture.resolve_areas([AreaSpec("St. Petersburg", "locality", 1, "US-FL")], "local")
    assert missing == [] and areas[0].boxes == [(-82.8, 27.65, -82.6, 27.9)]
    write_divisions(overture.settings.overture_divisions_source,
                    [division(name="St. Cloud", box=(-81.3, 28.2, -81.2, 28.3))])
    areas, missing = overture.resolve_areas([AreaSpec("Saint Cloud", "locality", 1, "US-FL")], "local")
    assert missing == [] and len(areas) == 1


def test_resolve_reports_unresolved(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [division()])
    assert overture.resolve_areas([AreaSpec("Gotham", "locality", 1, "US-FL")], "local") == ([], ["Gotham"])


def test_country_resolves_to_its_region_boxes_with_country_filter(overture_files):
    _, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="us", name="United States", subtype="country", region=None, box=(-179.9, 18.9, 179.9, 71.4)),
        division(id="fl", name="Florida", subtype="region", box=FL_BOX),
        division(id="ga", name="Georgia", subtype="region", region="US-GA", box=GA_BOX),
        division(id="on", name="Ontario", subtype="region", country="CA", region="CA-ON", box=(-95, 41, -74, 57)),
    ])
    areas, _ = overture.resolve_areas([AreaSpec("United States", "country", 1)], "local")
    assert sorted(areas[0].boxes) == sorted([FL_BOX, GA_BOX]) and areas[0].country_code == "US"
    assert areas[0].region_values is None


def test_region_area_accepts_code_name_or_empty_region(overture_files):
    write_places, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source,
                    [division(name="Florida", subtype="region", box=FL_BOX)])
    areas, _ = overture.resolve_areas([AreaSpec("Florida", "region", 1, "US-FL")], "local")
    assert areas[0].region_values == {"FL", "Florida"}
    write_places(overture.settings.overture_places_source, [
        place(id="code", region="FL"), place(id="name", region="Florida"),
        place(id="none", region=None), place(id="empty", region=""), place(id="ga", region="GA"),
    ])
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5)
    assert sorted(r["source_id"] for r in rows) == ["code", "empty", "name", "none"]


def test_fetch_applies_filters(overture_files):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [
        place(id="keep"),
        place(id="closed", operating_status="permanently_closed"),
        place(id="lowconf", confidence=0.3),
        place(id="nosite", websites=None),
        place(id="hvac", category="hvac_service"),
        place(id="orlando", lon=-81.38, lat=28.54),
    ])
    rows = overture.fetch_places("local", ["dental_clinic"], [area()], 0.5)
    assert [r["source_id"] for r in rows] == ["keep"]
    r = rows[0]
    assert r["website_url"] == "https://tampadental.com" and r["region"] == "FL"
    assert r["name"] == "Tampa Dental" and r["category"] == "dental_clinic"
    assert r["emails"] == ["info@tampadental.com"] and r["phones"] == ["+18135550100"]
    assert r["brand"] is None and r["confidence"] == 0.9
    assert (r["city"], r["country"], r["lat"], r["lon"], r["tier"]) == ("Tampa", "US", 27.95, -82.46, 1)


def test_fetch_country_filter_and_brand(overture_files):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [
        place(id="us", brand={"names": {"primary": "Acme"}}), place(id="ca", country="CA"),
    ])
    rows = overture.fetch_places("local", ["dental_clinic"], [area(country_code="US")], 0.5)
    assert [(r["source_id"], r["brand"]) for r in rows] == [("us", "Acme")]


def test_fetch_tier_is_best_containing_area(overture_files):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [place(id="x")])
    areas = [area("Florida", 2, FL_BOX, kind="region"), area("Tampa", 1)]
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5)
    assert len(rows) == 1 and rows[0]["tier"] == 1


def test_fetch_wraps_duckdb_errors(overture_files):
    with pytest.raises(overture.OvertureError):
        overture.fetch_places("local", ["dental_clinic"], [area()], 0.5)
