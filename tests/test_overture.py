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


@pytest.fixture
def sql_rows(monkeypatch):
    """Records every row DuckDB hands back to Python, so tests can tell SQL filtering
    apart from filtering done afterwards in Python."""
    seen = []
    real = overture._connect

    class Recorder:
        def __init__(self, con):
            self.con = con

        def execute(self, *a):
            self.con.execute(*a)
            return self

        def fetchall(self):
            rows = self.con.fetchall()
            seen.extend(rows)
            return rows

        def fetchmany(self, *a):
            rows = self.con.fetchmany(*a)
            seen.extend(rows)
            return rows

        def fetchone(self):
            return self.con.fetchone()

        def close(self):
            self.con.close()

    monkeypatch.setattr(overture, "_connect", lambda *src: Recorder(real(*src)))
    return seen


def test_country_and_region_filters_run_in_sql(overture_files, sql_rows):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [
        place(id="us"), place(id="nocountry", country=None), place(id="ca", country="CA"),
        place(id="ga", region="GA"), place(id="blank", region=""),
    ])
    rows = overture.fetch_places("local", ["dental_clinic"],
                                 [area("Florida", 1, FL_BOX, {"FL", "Florida"}, "US", kind="region")], 0.5)
    assert sorted(r["source_id"] for r in rows) == ["blank", "nocountry", "us"]
    assert sorted(r[0] for r in sql_rows) == ["blank", "nocountry", "us"]


ALASKA_ROWS = [(-180.0, 51.0, 180.0, 71.6), (-179.1, 51.2, 179.8, 71.4)]


def test_country_collapses_duplicate_region_rows_and_filters_wide_boxes(overture_files, sql_rows):
    write_places, write_divisions = overture_files
    write_divisions(overture.settings.overture_divisions_source, [
        division(id="us", name="United States", subtype="country", region=None, box=(-179.9, 18.9, 179.9, 71.4)),
        division(id="ak1", name="Alaska", subtype="region", region="US-AK", box=ALASKA_ROWS[0]),
        division(id="ak2", name="Alaska", subtype="region", region="US-AK", box=ALASKA_ROWS[1]),
        division(id="fl", name="Florida", subtype="region", box=FL_BOX),
    ])
    areas, _ = overture.resolve_areas([AreaSpec("United States", "country", 1)], "local")
    assert sorted(areas[0].boxes) == sorted([ALASKA_ROWS[0], FL_BOX])
    write_places(overture.settings.overture_places_source, [
        place(id="anchorage", lon=-149.9, lat=61.2, region="AK"),
        place(id="oslo", lon=10.7, lat=59.9, country="NO", region="03"),
        place(id="oslo-nocountry", lon=10.7, lat=59.9, country=None, region="03"),
        place(id="tampa"),
    ])
    sql_rows.clear()  # drop the division lookups recorded by resolve_areas
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5)
    assert sorted(r["source_id"] for r in rows) == ["anchorage", "tampa"]
    assert sorted(r[0] for r in sql_rows) == ["anchorage", "tampa"]


def _cap_world(write_places):
    write_places(overture.settings.overture_places_source, [
        place(id="t-60", confidence=0.6), place(id="t-90", confidence=0.9),
        place(id="t-70", confidence=0.7), place(id="t-80", confidence=0.8),
        place(id="orl-99", confidence=0.99, lon=-81.38, lat=28.54, locality="Orlando"),
    ])
    return [area("Tampa", 1), area("Florida", 2, FL_BOX, {"FL", "Florida"}, "US", kind="region")]


def test_fetch_cap_keeps_best_ranked_and_says_so(overture_files, monkeypatch):
    write_places, _ = overture_files
    areas = _cap_world(write_places)
    monkeypatch.setattr(overture.settings, "overture_max_places", 3)
    msgs = []
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5, messages=msgs)
    assert [(r["source_id"], r["tier"]) for r in rows] == [("t-90", 1), ("t-80", 1), ("t-70", 1)]
    assert msgs == ["More than 3 businesses matched. Kept the 3 best-ranked. "
                    "Narrow the area or business types to see others."]


def test_fetch_under_cap_returns_all_ordered_without_message(overture_files, monkeypatch):
    write_places, _ = overture_files
    areas = _cap_world(write_places)
    monkeypatch.setattr(overture.settings, "overture_max_places", 5)
    msgs = []
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5, messages=msgs)
    assert [r["source_id"] for r in rows] == ["t-90", "t-80", "t-70", "t-60", "orl-99"]
    assert msgs == []


def test_cap_message_uses_thousands_separator(overture_files, monkeypatch):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source,
                 [place(id=f"p{i}", confidence=0.5 + i / 10000) for i in range(1001)])
    msgs = []
    rows = overture.fetch_places("local", ["dental_clinic"], [area()], 0.5, max_places=1000, messages=msgs)
    assert len(rows) == 1000 and rows[0]["source_id"] == "p1000"
    assert msgs[0].startswith("More than 1,000 businesses matched. Kept the 1,000 best-ranked.")


def test_place_in_two_areas_counts_once_at_best_tier_before_cap(overture_files, monkeypatch):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [
        place(id="both", confidence=0.5),  # in Tampa (tier 1) and Florida (tier 2)
        place(id="orl-99", confidence=0.99, lon=-81.38, lat=28.54),
        place(id="orl-98", confidence=0.98, lon=-81.38, lat=28.54),
    ])
    areas = [area("Florida", 2, FL_BOX, {"FL", "Florida"}, "US", kind="region"), area("Tampa", 1)]
    msgs = []
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5, max_places=3, messages=msgs)
    assert [(r["source_id"], r["tier"]) for r in rows] == [("both", 1), ("orl-99", 2), ("orl-98", 2)]
    assert msgs == []  # 3 distinct places, not 4 rows
    rows = overture.fetch_places("local", ["dental_clinic"], areas, 0.5, max_places=2, messages=msgs)
    assert [(r["source_id"], r["tier"]) for r in rows] == [("both", 1), ("orl-99", 2)]
    assert len(msgs) == 1 and msgs[0].startswith("More than 2 businesses matched.")


def test_place_query_streams_without_sql_top_n(overture_files, monkeypatch):
    write_places, _ = overture_files
    write_places(overture.settings.overture_places_source, [place()])
    calls = []
    real = overture._connect

    class Spy:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, params=None):
            calls.append(("execute", sql))
            self.con.execute(sql, params)
            return self

        def fetchall(self):
            calls.append(("fetchall", None))
            return self.con.fetchall()

        def fetchmany(self, *a):
            calls.append(("fetchmany", None))
            return self.con.fetchmany(*a)

        def close(self):
            self.con.close()

    monkeypatch.setattr(overture, "_connect", lambda *src: Spy(real(*src)))
    assert len(overture.fetch_places("local", ["dental_clinic"], [area()], 0.5)) == 1
    sql = next(q for kind, q in calls if kind == "execute")
    assert "QUALIFY" not in sql and "ORDER BY" not in sql and "LIMIT" not in sql
    assert ("fetchall", None) not in calls and ("fetchmany", None) in calls


def _rank(r):  # test rows: (id, confidence, tier)
    return (r[2], -r[1], r[0])


def test_best_ranked_dedupes_then_caps_with_bounded_memory():
    # "x" first streams at tier 2 and is pruned, then returns at tier 1 with the best rank;
    # "y" is pruned at tier 1 and later returns at tier 2: it must not come back.
    chunks = [
        [("x", 0.99, 2), ("y", 0.2, 1)],
        [(f"f{i}", 0.5 + i / 100, 1) for i in range(10)],  # 12 ids > 2*3: prune to f9, f8, f7
        [("x", 0.99, 1), ("y", 0.2, 2), ("f9", 0.59, 2)],
    ]
    rows = overture._best_ranked(iter(chunks), 3, _rank)
    assert rows == [("x", 0.99, 1), ("f9", 0.59, 1), ("f8", 0.58, 1)]
    rows = overture._best_ranked(iter(chunks), 0, _rank)  # no cap: every id once, at its best rank
    assert len(rows) == 12 and ("x", 0.99, 1) in rows and ("y", 0.2, 1) in rows
    assert ("x", 0.99, 2) not in rows and ("f9", 0.59, 2) not in rows


def test_best_ranked_holds_at_most_twice_the_cap_plus_a_chunk(monkeypatch):
    sizes = []
    real_nsmallest = overture.heapq.nsmallest

    def spy(n, it, key):
        it = list(it)
        sizes.append(len(it))
        return real_nsmallest(n, it, key=key)

    monkeypatch.setattr(overture.heapq, "nsmallest", spy)
    chunks = ([(f"p{c}-{i}", (c * 100 + i) / 10000, 1) for i in range(100)] for c in range(50))
    rows = overture._best_ranked(chunks, 10, _rank)
    assert rows[0] == ("p49-99", 0.4999, 1) and len(rows) == 10
    assert sizes and max(sizes) <= 2 * 10 + 100
