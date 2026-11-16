# Overture Company Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LLM-recall and Bing/DuckDuckGo company discovery with live
searches of Overture Maps Places. Every result is kept as a per-campaign
pool, and batches are processed in parallel under a "real emails only" lead
rule.

**Architecture:**
- A thin DuckDB layer (`app/engine/overture.py`) reads Overture's public
  parquet files, or local fixture files in tests.
- An LLM search plan (`search_plan.py`) turns the ICP into categories plus
  areas.
- `company_pool.py` cleans results and stores the pool in SQLite.
- `lead_builder.py` turns pool rows into leads in bounded-parallel batches.
- `discovery.py` orchestrates the search.
- `campaigns.py` wires it into the existing API, and the UI shows the plan
  and pool.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, DuckDB (`httpfs`),
pytest + pytest-asyncio, vanilla JS in `app/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-11-16-overture-company-discovery-design.md`

## Global Constraints

- Python 3.12 (Docker base `python:3.12-slim`). Add `duckdb` to
  `requirements.txt`. Install the `httpfs` extension when the image is
  built. Never use the `spatial` extension.
- Settings and defaults: `OVERTURE_RELEASE=""` (latest),
  `OVERTURE_MIN_CONFIDENCE=0.5`, `DISCOVERY_BATCH_SIZE=50`,
  `DISCOVERY_CONCURRENCY=8`, `DEFAULT_SEARCH_AREA="United States"`.
- Overture base: `s3://overturemaps-us-west-2/release/<release>/`. Places
  are at `theme=places/type=place/*`, areas at
  `theme=divisions/type=division_area/*`. Anonymous access,
  `s3_region='us-west-2'`. Cache the latest release for 24 h.
- Run every DuckDB call off the event loop (`asyncio.to_thread`).
- Campaign discovery never calls the LLM for company names
  (`_suggest_domains_via_llm` and `ICP_CURATED_DOMAINS` are deleted), and
  never calls Bing or DuckDuckGo. Serper is used only for non-local ICPs,
  and only when `SERPER_API_KEY` is set.
- Chain rule: drop places with a brand, and drop any domain used by 4 or
  more places in a fetch.
- Lead rule:
  - A found own-domain address is accepted unless it verifies `invalid`.
  - A guessed person address is accepted only if it verifies `verified` or
    `deliverable`.
  - There is no `contact@`/`hello@` fallback.
  - Skip reasons are exactly `site_unreachable`, `no_email`,
    `email_invalid` and `in_other_campaign`.
- Attribution copy: "Business data: Overture Maps Foundation
  (CDLA-Permissive-2.0 / Apache-2.0 / CC0)". It goes in the UI footer and
  the README.
- Only additive schema changes, through `app/database.py`'s existing
  `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN` list.
- `python tests/test_cloudflare_and_settings.py` must keep printing
  `ALL CHECKS PASSED!`.

## Review Focus

- **Overture/S3 unreachable or a DuckDB error mid-search:** the campaign
  ends `error` with a readable `discovery_message`, never stuck in
  `discovering`. Test in Task 7.
- **The AI provider returns nothing or garbage for the plan:** a readable
  `PlanError`, not a crash. Test in Task 3.
- **Two "Find more" clicks or overlapping batches:** no pool row is
  processed twice, because claiming is atomic. Test in Task 6.
- **Nationwide pools of tens of thousands of rows:** `save_pool` inserts in
  chunks and the counters stay right. Test in Task 4.
- **Messy Overture website values** (no scheme, uppercase, `www.`, paths,
  ports, social links, empty strings): domains are normalized, unusable
  ones dropped. Test in Task 4.

## File Structure

| File | Responsibility |
|---|---|
| `app/engine/overture.py` (new) | Release detection, category list, area resolution, place fetch. Sync DuckDB. |
| `app/engine/search_plan.py` (new) | `SearchPlan`/`AreaSpec` types, LLM plan building and validation. |
| `app/engine/company_pool.py` (new) | Domain normalization, cleaning rules, held/suppressed domains, pool persistence and counters. |
| `app/engine/lead_builder.py` (new) | Lead email rule, per-company enrichment, batch claiming and parallel processing. |
| `app/engine/discovery.py` (modify) | Delete LLM-recall discovery. Add `search_companies_for_icp`, `search_web_candidates` and `refresh_campaign_pool`. Keep the Bing/DDG/Serper and people functions. |
| `app/api/campaigns.py` (modify) | New task flow, the refresh endpoint, batch size on create/autopilot, parsed plan in the list. |
| `app/api/mcp.py` (modify) | `search_companies` uses the new engine. |
| `app/config.py`, `app/database.py` (modify) | Settings and schema. |
| `app/static/index.html`, `README.md`, `.env.example` (modify) | Plan/pool UI, create-form field, attribution, documented settings. |
| `requirements.txt`, `requirements-dev.txt` (new), `Dockerfile`, `pytest.ini` (new), `scripts/test.sh` (new), `tests/Dockerfile` (new), `tests/conftest.py` (new), `tests/overture_fixtures.py` (new) | Dependencies and an offline test harness. |

Run all tests with `scripts/test.sh` (offline, `--network none`, the
repo's `.env` hidden). Run the live test with
`OPENEXPLEE_LIVE=1 scripts/test.sh -m live`.

---

### Task 1: Settings, schema and offline test harness

**Files:**
- Modify: `requirements.txt`, `Dockerfile`, `app/config.py`, `app/database.py`
- Create: `requirements-dev.txt` (`pytest`, `pytest-asyncio`), `pytest.ini`
  (`asyncio_mode = auto`, `markers = live: hits the real Overture bucket`,
  `addopts = -m "not live"`), `scripts/test.sh`, `tests/Dockerfile`,
  `tests/conftest.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces:
  - Settings: `overture_release: str = ""`,
    `overture_min_confidence: float = 0.5`,
    `discovery_batch_size: int = 50`, `discovery_concurrency: int = 8`,
    `default_search_area: str = "United States"`,
    `serper_max_queries: int = 4`, `overture_places_source: str = ""`,
    `overture_divisions_source: str = ""` (local parquet globs; tests use
    them; blank means S3).
  - Fixture `db` (async): points `app.database.DB_FILE` at
    `tmp_path/"t.db"`, awaits `init_db()`, and yields the path.
  - Schema:
    - `campaigns` gets `search_plan TEXT`, `overture_release TEXT`,
      `pool_total INTEGER DEFAULT 0`, `pool_pending INTEGER DEFAULT 0`,
      `batch_size INTEGER DEFAULT 50`, `discovery_message TEXT`.
    - `leads` gets `phone TEXT`, `company_id INTEGER`.
    - New table `campaign_companies`: `id INTEGER PRIMARY KEY
      AUTOINCREMENT`, `campaign_id INTEGER REFERENCES campaigns(id) ON
      DELETE CASCADE`, `source TEXT NOT NULL DEFAULT 'overture'`,
      `source_id TEXT`, `name TEXT`, `category TEXT`, `domain TEXT NOT
      NULL`, `website_url TEXT`, `emails TEXT` (JSON list), `phones TEXT`
      (JSON list), `city TEXT`, `region TEXT`, `country TEXT`, `lat REAL`,
      `lon REAL`, `tier INTEGER DEFAULT 1`, `confidence REAL`, `status TEXT
      DEFAULT 'pending'`, `skip_reason TEXT`, `lead_id INTEGER`,
      `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`,
      `UNIQUE(campaign_id, domain)`, plus indexes on
      `(campaign_id, status, tier)` and `(domain)`.

- [ ] **Step 1: Build the harness.**
  - `Dockerfile`: after `pip install`, add
    `RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"`.
  - `tests/Dockerfile`: `FROM openexplee:local`, then copy
    `requirements-dev.txt` and pip install it.
  - `scripts/test.sh`:
    - build `openexplee:test` from `tests/Dockerfile`
    - run it with `--network none`, or without it when
      `OPENEXPLEE_LIVE=1`
    - pass `-e OPENEXPLEE_LIVE -e PYTHONDONTWRITEBYTECODE=1`
    - mount `-v "$PWD":/src -v /dev/null:/src/.env:ro -w /src`
    - run `python -m pytest -q -p no:cacheprovider "$@"`
  - `tests/conftest.py`: set `os.environ["DATABASE_PATH"]` to a temp file
    before any `app` import, and define the `db` fixture.

- [ ] **Step 2: Write the failing tests** in `tests/test_schema.py`:

```python
async def test_new_tables_and_columns(db):
    async with aiosqlite.connect(db) as c:
        cols = {r[1] for r in await (await c.execute("PRAGMA table_info(campaign_companies)")).fetchall()}
        assert {"campaign_id", "domain", "tier", "status", "skip_reason", "lead_id", "emails", "phones"} <= cols
        camp = {r[1] for r in await (await c.execute("PRAGMA table_info(campaigns)")).fetchall()}
        assert {"search_plan", "overture_release", "pool_total", "pool_pending", "batch_size", "discovery_message"} <= camp
        leads = {r[1] for r in await (await c.execute("PRAGMA table_info(leads)")).fetchall()}
        assert {"phone", "company_id"} <= leads

async def test_startup_resets_processing_rows(db):
    # insert campaign + a campaign_companies row with status 'processing', then await init_db()
    assert status_of_that_row == "pending"

async def test_pool_domain_unique_per_campaign(db):
    # inserting the same (campaign_id, domain) twice raises sqlite3.IntegrityError

def test_settings_defaults():
    from app.config import Settings
    s = Settings(_env_file=None)
    assert (s.discovery_batch_size, s.discovery_concurrency, s.overture_min_confidence, s.default_search_area) == (50, 8, 0.5, "United States")
```

- [ ] **Step 3: Rebuild and run them to see them fail.** Run
  `docker compose build && scripts/test.sh tests/test_schema.py`. Expected:
  FAIL (missing table/columns/settings).
- [ ] **Step 4: Implement the settings in `app/config.py` and the schema in
  `init_db()`.** On startup, run
  `UPDATE campaign_companies SET status='pending' WHERE status='processing'`
  next to the existing `discovering → error` reset.
- [ ] **Step 5: Run the tests to see them pass.**
  `scripts/test.sh tests/test_schema.py` should show 4 passed. Also run
  `docker run --rm -v "$PWD/tests":/app/tests:ro -w /app -e PYTHONPATH=/app openexplee:local python tests/test_cloudflare_and_settings.py`
  and expect `ALL CHECKS PASSED!`.
- [ ] **Step 6: Commit.**
  `git commit -m "feat: discovery settings, pool schema and offline test harness"`

---

### Task 2: Overture data layer

**Files:**
- Create: `app/engine/overture.py`, `tests/overture_fixtures.py`
- Test: `tests/test_overture.py`

**Interfaces:**
- Consumes: the Task 1 settings.
- Produces (all sync; callers wrap them in `asyncio.to_thread`):
  - `@dataclass class AreaSpec: name: str; kind: str; tier: int = 1; region_code: str | None = None; country_code: str | None = None`.
    `kind` is one of `locality|county|region|country`. `region_code` looks
    like `"US-FL"`.
  - `@dataclass class ResolvedArea: name: str; kind: str; tier: int; boxes: list[tuple[float, float, float, float]]; region_values: set[str] | None; country_code: str | None`.
    Boxes are `(xmin, ymin, xmax, ymax)`.
  - `class OvertureError(Exception)`
  - `get_release() -> str`: returns `settings.overture_release` if set,
    `"local"` if `overture_places_source` is set, and otherwise the max
    `release/<x>/` name found by globbing S3, cached 24 h.
  - `load_categories(release: str) -> list[str]`: the sorted distinct
    non-null `basic_category`. It's cached as JSON at
    `Path(settings.database_path).parent / "overture" / release / "categories.json"`.
  - `resolve_areas(specs: list[AreaSpec], release: str) -> tuple[list[ResolvedArea], list[str]]`:
    the resolved areas and the unresolved names.
  - `fetch_places(release: str, categories: list[str], areas: list[ResolvedArea], min_confidence: float) -> list[dict]`.
    Each dict has the keys `source_id`, `name`, `category`, `website_url`
    (first website), `emails`, `phones` (lists), `brand` (str|None),
    `confidence`, `city`, `region`, `country`, `lat`, `lon` and `tier`.
- Fixture helpers (`tests/overture_fixtures.py`):
  - `write_places(path, rows: list[dict])` and `write_divisions(path, rows: list[dict])`
    write parquet with DuckDB using Overture's nested shape:
    - places: `id VARCHAR, names STRUCT("primary" VARCHAR), basic_category VARCHAR, taxonomy STRUCT("primary" VARCHAR), websites VARCHAR[], emails VARCHAR[], phones VARCHAR[], brand STRUCT(names STRUCT("primary" VARCHAR)), confidence DOUBLE, operating_status VARCHAR, addresses STRUCT(freeform VARCHAR, locality VARCHAR, postcode VARCHAR, region VARCHAR, country VARCHAR)[], bbox STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE)`
    - divisions: `id VARCHAR, names STRUCT("primary" VARCHAR), subtype VARCHAR, country VARCHAR, region VARCHAR, bbox STRUCT(...)`
  - `place(**overrides) -> dict`: a default open Tampa `dental_clinic` with
    `https://tampadental.com`, confidence 0.9, region `FL`, country `US`
    and point `(-82.46, 27.95)`.
  - `division(**overrides) -> dict`
  - Fixture `overture_files(tmp_path, monkeypatch)`: sets both
    `overture_*_source` settings to the written files and returns the
    writer callables.

- [ ] **Step 1: Write the failing tests** in `tests/test_overture.py`:

```python
def test_get_release_local_when_sources_overridden(overture_files): assert overture.get_release() == "local"

def test_load_categories_distinct_sorted_and_cached(overture_files, monkeypatch):
    # places with categories dental_clinic, hvac_service, dental_clinic, None
    assert overture.load_categories("local") == ["dental_clinic", "hvac_service"]
    monkeypatch.setattr(overture.duckdb, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not cached")))
    assert overture.load_categories("local") == ["dental_clinic", "hvac_service"]

def test_resolve_merges_duplicate_rows_into_union_box(overture_files):
    # two county rows "Pinellas County"/US-FL with boxes (-82.909,27.610,-82.541,28.173) and (-82.852,27.611,-82.583,28.173)
    areas, missing = overture.resolve_areas([AreaSpec("Pinellas County", "county", 1, "US-FL")], "local")
    assert missing == [] and areas[0].boxes == [(-82.909, 27.610, -82.541, 28.173)]

def test_resolve_disambiguates_by_region_code(overture_files):  # Hillsborough County US-FL vs US-NH -> only the FL box
def test_resolve_retries_saint_st_variant(overture_files):     # spec "St. Petersburg" resolves division named "Saint Petersburg"
def test_resolve_reports_unresolved(overture_files):           # "Gotham" -> ([], ["Gotham"])
def test_country_resolves_to_its_region_boxes_with_country_filter(overture_files):
    # divisions: US country row with box (-179.9,18.9,179.9,71.4); regions FL and GA
    areas, _ = overture.resolve_areas([AreaSpec("United States", "country", 1)], "local")
    assert sorted(areas[0].boxes) == sorted([FL_BOX, GA_BOX]) and areas[0].country_code == "US"
def test_region_area_accepts_code_name_or_empty_region(overture_files):
    # region area FL: region_values == {"FL", "Florida"}; fetch keeps places with region "FL", "Florida", None; drops "GA"

def test_fetch_applies_filters(overture_files):
    # keep: default place; drop: operating_status 'permanently_closed', confidence 0.3, websites None,
    # basic_category 'hvac_service' (not in plan), point in Orlando (-81.38, 28.54)
    rows = overture.fetch_places("local", ["dental_clinic"], [tampa_area], 0.5)
    assert [r["source_id"] for r in rows] == ["keep"]
    assert rows[0]["website_url"] == "https://tampadental.com" and rows[0]["region"] == "FL"

def test_fetch_tier_is_best_containing_area(overture_files):
    # place inside a tier-2 Florida area and a tier-1 Tampa area -> tier == 1, returned once

def test_fetch_wraps_duckdb_errors(overture_files, monkeypatch):
    # make the source glob point at a missing file -> raises overture.OvertureError
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_overture.py`
  fails with an ImportError.
- [ ] **Step 3: Implement `app/engine/overture.py`.**
  - Use a new `duckdb.connect()` per call. Run
    `LOAD httpfs; SET s3_region='us-west-2'` only when the source starts
    with `s3://`.
  - Area lookup:
    - match `lower(names.primary) = lower(name)`
    - `subtype` from `{locality: ('locality','localadmin'), county: ('county',), region: ('region',), country: ('country',)}`
    - when given, `region = region_code` and/or `country = country_code`
    - if there's no match and the name starts with `St.`/`St ` or
      `Saint `, retry once with the other form
  - `country` areas: `boxes` = boxes of that country's `region` rows,
    `country_code` = the country row's `country`, `region_values=None`.
  - `region` areas: `region_values = {region_code.split('-')[1], name}`.
  - Fetch: one `SELECT` per box, joined with `UNION ALL`, so each box
    prunes row groups. Point = `bbox.xmin`/`bbox.ymin`. Filters:
    - `basic_category IN (...)`
    - `operating_status IS DISTINCT FROM 'permanently_closed'`
    - `confidence >= ?`
    - `len(websites) > 0`

    Then post-filter `region_values` (keep null/empty regions) and
    `country_code` (keep null countries). Dedupe by `source_id`, keeping
    the lowest tier.
  - Wrap every `duckdb.Error` in `OvertureError(str(e))`.
- [ ] **Step 4: Run them to see them pass.** `scripts/test.sh tests/test_overture.py`
  shows all passed.
- [ ] **Step 5: Commit.**
  `git commit -m "feat: Overture data layer (release, categories, areas, places)"`

---

### Task 3: Search plan

**Files:**
- Create: `app/engine/search_plan.py`
- Test: `tests/test_search_plan.py`

**Interfaces:**
- Consumes: `AreaSpec` (Task 2), `app.engine.llm.call_llm_json`.
- Produces:
  - `@dataclass class SearchPlan: categories: list[str]; areas: list[AreaSpec]; is_local_business: bool; messages: list[str]`
  - `def plan_to_json(plan: SearchPlan) -> str` and `def plan_from_json(s: str) -> SearchPlan`
  - `class PlanError(Exception)`
  - `async def build_search_plan(icp_prompt: str, categories: list[str]) -> SearchPlan`

- [ ] **Step 1: Write the failing tests** in `tests/test_search_plan.py`.
  Monkeypatch `search_plan.call_llm_json` in each.

```python
CATS = ["dental_clinic", "chiropractic", "hvac_service", "software_development"]

async def test_unknown_categories_dropped_and_areas_parsed(mp_llm):
    mp_llm({"categories": ["dental_clinic", "Chiropractic", "made_up"], "is_local_business": True,
            "areas": [{"name": "Pinellas County", "kind": "county", "region_code": "US-FL", "tier": 1},
                      {"name": "Florida", "kind": "region", "region_code": "US-FL", "tier": 2}]})
    p = await build_search_plan("dentists and chiropractors, Pinellas first then Florida", CATS)
    assert p.categories == ["dental_clinic", "chiropractic"]
    assert [(a.name, a.kind, a.tier) for a in p.areas] == [("Pinellas County", "county", 1), ("Florida", "region", 2)]

async def test_no_place_uses_default_area_and_says_so(mp_llm, monkeypatch):
    mp_llm({"categories": ["software_development"], "is_local_business": False, "areas": []})
    p = await build_search_plan("SaaS startups using Stripe", CATS)
    assert [(a.name, a.kind) for a in p.areas] == [("United States", "country")]
    assert any("United States" in m for m in p.messages)

async def test_no_valid_category_for_local_icp_raises(mp_llm):
    mp_llm({"categories": ["made_up"], "is_local_business": True, "areas": [{"name": "Tampa", "kind": "locality"}]})
    with pytest.raises(PlanError, match="categor"): await build_search_plan("x", CATS)

async def test_no_category_ok_for_non_local_icp(mp_llm):  # returns plan with categories == [], is_local_business False

async def test_llm_returns_nothing_raises_readable_error(mp_llm):   # Review Focus
    for bad in (None, "garbage", [], {"areas": "nope"}):
        mp_llm(bad)
        with pytest.raises(PlanError): await build_search_plan("x", CATS)

def test_plan_json_roundtrip(): assert plan_from_json(plan_to_json(PLAN)) == PLAN
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_search_plan.py`
  fails with an ImportError.
- [ ] **Step 3: Implement `build_search_plan`.**
  - The prompt includes the full category list, the JSON shape
    (`categories`, `areas[name, kind, region_code, country_code, tier]`,
    `is_local_business`), and instructions:
    - use official place names
    - map priority wording to tiers
    - set `is_local_business=false` for businesses without a public
      location
  - Normalize category case and spaces to snake_case before intersecting.
  - Defaults: an invalid `kind` drops that area; a missing `tier` becomes
    1.
  - Error copy:
    - `PlanError("Couldn't build a search plan: the AI provider returned nothing usable.")`
    - `PlanError("None of the business types in the target matched Overture's categories.")`
- [ ] **Step 4: Run them to see them pass.** `scripts/test.sh tests/test_search_plan.py`
- [ ] **Step 5: Commit.** `git commit -m "feat: LLM search plan with validation"`

---

### Task 4: Cleaning rules and pool storage

**Files:**
- Create: `app/engine/company_pool.py`
- Test: `tests/test_company_pool.py`

**Interfaces:**
- Consumes: the `fetch_places` dict shape (Task 2), the `db` fixture.
- Produces:
  - `PLATFORM_HOSTS: tuple[str, ...]`: substrings of social, platform and
    directory hosts. At least facebook, instagram, linkedin, yelp,
    linktr.ee, google, twitter, x.com, tiktok, youtube, wixsite,
    godaddysites, square.site, business.site, hub.biz.
  - `FRANCHISE_DOMAINS: frozenset[str]`: seeded with the national chains
    from the Explee log, e.g. servpro.com, aspendental.com,
    anytimefitness.com, greatclips.com, jiffylube.com, massageenvy.com,
    statefarm.com, allstate.com, molly maid and similar.
  - `CHAIN_DOMAIN_MIN = 4`
  - `def normalize_domain(url: str | None) -> str`: returns `""` if
    unusable.
  - `def clean_candidates(places: list[dict], held: set[str], suppressed: set[str]) -> list[dict]`:
    adds a `"domain"` key, sorted by `(tier, -confidence)`.
  - `async def suppressed_domains() -> set[str]`: moved from
    `discovery._get_suppressed_domains`. Keep a re-export there.
  - `async def held_domains(exclude_campaign_id: int | None) -> set[str]`:
    the domains of leads, plus `pending|processing|done` pool rows, in
    other campaigns.
  - `async def save_pool(campaign_id: int, companies: list[dict], source: str) -> int`:
    `INSERT OR IGNORE` in chunks of 500. Returns the number inserted and
    refreshes the counters.
  - `async def refresh_pool_counters(campaign_id: int) -> None`: sets
    `pool_total` (all rows) and `pool_pending` (`pending`).

- [ ] **Step 1: Write the failing tests** in `tests/test_company_pool.py`:

```python
@pytest.mark.parametrize("raw,expected", [
    ("https://www.TampaDental.com/contact?x=1", "tampadental.com"),
    ("tampadental.com", "tampadental.com"), ("http://tampadental.com:8080/", "tampadental.com"),
    ("", ""), (None, ""), ("https://facebook.com/tampadental", "facebook.com"), ("not a url", ""),
])
def test_normalize_domain(raw, expected): assert normalize_domain(raw) == expected   # Review Focus

def test_clean_drops_brand_platform_chain_franchise_held_suppressed():
    places = [p("ok.com"), p("brand.com", brand="Brand"), p("https://facebook.com/x"), p("https://hub.biz/x"),
              *[p("chain.com", source_id=f"c{i}") for i in range(4)], p("servpro.com"),
              p("held.com"), p("supp.com"), p("")]
    out = clean_candidates(places, held={"held.com"}, suppressed={"supp.com"})
    assert [c["domain"] for c in out] == ["ok.com"]

def test_clean_dedupes_by_domain_keeping_highest_confidence_and_orders_by_tier():
    out = clean_candidates([p("a.com", confidence=0.6, tier=2), p("a.com", confidence=0.9, tier=2), p("b.com", tier=1)], set(), set())
    assert [(c["domain"], c["confidence"]) for c in out] == [("b.com", 0.9), ("a.com", 0.9)]

def test_three_locations_is_not_a_chain():  # 3 places on one domain -> kept once

async def test_held_domains_covers_other_campaigns_leads_and_pool(db):
    # campaign 1: lead x.com, pool pending y.com, pool skipped z.com; campaign 2 asks
    assert await held_domains(2) == {"x.com", "y.com"}

async def test_save_pool_ignores_duplicates_and_updates_counters(db):
    assert await save_pool(cid, [c("a.com"), c("b.com")], "overture") == 2
    assert await save_pool(cid, [c("a.com"), c("c.com")], "overture") == 1
    assert (pool_total, pool_pending) == (3, 3)

async def test_save_pool_large_batch(db):   # Review Focus
    assert await save_pool(cid, [c(f"d{i}.com") for i in range(20000)], "overture") == 20000
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_company_pool.py`
- [ ] **Step 3: Implement `company_pool.py`.**
  - `normalize_domain`: add `https://` when the scheme is missing, then
    `urlparse`, lowercase the host, strip `www.` and the port. Return
    `""` when the host has no dot or contains spaces.
  - Platform and directory hosts are dropped in `clean_candidates`, not in
    `normalize_domain`.
  - Store `emails`/`phones` as `json.dumps` lists.
- [ ] **Step 4: Run them to see them pass.** `scripts/test.sh tests/test_company_pool.py`
- [ ] **Step 5: Commit.** `git commit -m "feat: company cleaning rules and per-campaign pool"`

---

### Task 5: Search orchestration (Overture + Serper), replacing LLM-recall discovery

**Files:**
- Modify: `app/engine/discovery.py`
- Modify: `app/api/mcp.py` (`search_companies`)
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: Tasks 2–4. The existing `_search_queries_for_icp`,
  `search_serper` and `NON_COMPANY_HOSTS`.
- Produces:
  - `async def search_web_candidates(icp_prompt: str) -> list[dict]`:
    returns `[]` when there's no `serper_api_key`. Otherwise it takes the
    first `serper_max_queries` queries and returns candidate dicts in the
    `fetch_places` shape: `tier=1`, `confidence=0.5`, empty location,
    `source_id=url`. Hosts in `NON_COMPANY_HOSTS` are removed.
  - `async def search_companies_for_icp(icp_prompt: str, plan: SearchPlan | None = None) -> tuple[SearchPlan, list[dict], list[str], str]`:
    returns the plan, the candidates cleaned with the empty held set and
    the suppressed set, the user-facing messages, and the release.
    - `plan=None` builds a new plan.
    - All Overture calls go through `asyncio.to_thread`.
    - `PlanError`/`OvertureError` propagate.
    - Messages are added for unresolved area names, and for a non-local
      ICP without a Serper key, using exactly the spec's copy: "This target
      isn't a local business type. Open map data covers it poorly. Add a
      Serper key in Settings for web search results."
    - It raises `PlanError("None of the places in the target could be found: …")`
      when every area is unresolved.
  - `async def refresh_campaign_pool(campaign_id: int) -> dict`:
    - uses the stored `search_plan` if there is one, and builds and stores
      one otherwise
    - cleans against `held_domains(campaign_id)`
    - saves each source (`overture` and `serper`) separately
    - stores `overture_release` and `discovery_message` (the messages
      joined with `"\n"`)
    - returns `{"inserted": int, "pool_total": int, "messages": list[str]}`
  - Deleted: `discover_leads_for_icp`, `_suggest_domains_via_llm`,
    `ICP_CURATED_DOMAINS`.

- [ ] **Step 1: Write the failing tests** in `tests/test_discovery.py`.
  Use the Task 2 fixtures, the `db` fixture, a monkeypatched plan builder,
  and monkeypatched `search_serper`/`search_bing`/`search_duckduckgo` that
  record their calls.

```python
async def test_local_icp_uses_overture_only(...):
    plan, cands, msgs, rel = await search_companies_for_icp("dentists in Tampa, FL")
    assert [c["domain"] for c in cands] == ["tampadental.com"] and rel == "local"
    assert serper_calls == bing_calls == ddg_calls == []

async def test_non_local_without_serper_key_warns(...):
    assert "Open map data covers it poorly" in " ".join(msgs) and bing_calls == ddg_calls == []

async def test_non_local_with_serper_key_adds_web_candidates(...):  # settings.serper_api_key="k"; 2 serper results, one linkedin -> 1 candidate

async def test_refresh_reuses_stored_plan_and_only_adds_new(db, ...):
    r1 = await refresh_campaign_pool(cid)          # builds plan (1 LLM call)
    add_place_to_fixture("newdental.com")
    r2 = await refresh_campaign_pool(cid)
    assert plan_builder_calls == 1 and r2["inserted"] == 1 and no rows in leads

async def test_refresh_skips_domains_held_by_other_campaigns(db, ...)
async def test_unresolved_areas_reported_and_all_unresolved_raises(...)

def test_llm_recall_is_gone():
    import app.engine.discovery as d
    assert not hasattr(d, "_suggest_domains_via_llm") and not hasattr(d, "ICP_CURATED_DOMAINS")
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_discovery.py`
- [ ] **Step 3: Implement the orchestration functions, delete the old
  discovery code, and switch MCP.** MCP `search_companies` calls
  `search_companies_for_icp(args["definition"])` and returns the first
  `limit` candidates, with keys `name, domain, website_url, category,
  city, region, emails, phones`. Its description becomes "Find real
  companies matching a natural-language target from open business data
  (Overture Maps), plus web search for non-local businesses."
- [ ] **Step 4: Run all the tests.** `scripts/test.sh` shows all passed.
- [ ] **Step 5: Commit.**
  `git commit -m "feat: Overture-backed company search; remove LLM-recall discovery"`

---

### Task 6: Lead builder, the lead rule and parallel batches

**Files:**
- Create: `app/engine/lead_builder.py`
- Test: `tests/test_lead_builder.py`

**Interfaces:**
- Consumes:
  - the pool rows from Task 4 (`campaign_companies`)
  - `scrape_company_website(url) -> dict` (keys `domain, url, title,
    description, content_summary, found_emails, error?`)
  - `discovery._extract_people_with_llm(domain, text, job_titles=None) -> list[dict]`
  - `enrich_person_email(first, last, domain) -> {email, status, details}`
  - `verify_email_smtp(email, timeout) -> {status, details}`
  - `generate_cold_email(...) -> {subject, body}`
- Produces:
  - `def pick_lead_email(person: dict | None, found: list[tuple[str, str]]) -> tuple[str | None, str, str]`.
    `found` is `[(email, verification_status)]`. Returns
    `(email, verification_status, "")` for a lead, or
    `(None, "", skip_reason)`.
  - `async def enrich_company(company: dict, value_prop: str) -> dict`:
    returns `{"lead": {...}}` or `{"skip_reason": str}`. The lead dict has
    the `leads` columns plus `phone` and `company_id`.
  - `async def claim_pending(campaign_id: int, n: int) -> list[dict]`
  - `async def process_next_batch(campaign_id: int, batch_size: int, value_prop: str) -> dict`:
    returns `{"processed": int, "leads": int, "skipped": int, "lead_ids": list[int]}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_lead_builder.py`.
  Mock the scraper, people extraction, SMTP and copywriter.

```python
def test_person_guess_needs_deliverable():
    assert pick_lead_email({"email": "jo@a.com", "status": "deliverable"}, []) == ("jo@a.com", "deliverable", "")
    assert pick_lead_email({"email": "jo@a.com", "status": "catch_all"}, []) == (None, "", "no_email")
def test_found_address_accepted_when_risky_rejected_when_invalid():
    assert pick_lead_email(None, [("info@a.com", "risky")]) == ("info@a.com", "risky", "")
    assert pick_lead_email(None, [("info@a.com", "invalid")]) == (None, "", "email_invalid")
def test_verified_person_preferred_over_found_role_address():
    assert pick_lead_email({"email": "jo@a.com", "status": "verified"}, [("info@a.com", "risky")])[0] == "jo@a.com"
def test_no_contact_fallback(): assert pick_lead_email(None, []) == (None, "", "no_email")

async def test_unreachable_site_is_skipped(...):        # scraper returns {"error": "HTTP 503", ...} -> {"skip_reason": "site_unreachable"}
async def test_found_emails_limited_to_own_domain_plus_overture(...)  # site finds info@a.com and x@gmail.com; Overture has office@a.com
async def test_copy_drafted_only_for_leads(...)          # generate_cold_email not called on skip

async def test_batch_marks_rows_and_counts(db, ...):
    # pool of 5: 3 become leads, 2 skip -> rows done/skipped with reasons, leads.company_id set, pool_pending == 0
    assert await process_next_batch(cid, 5, "vp") == {"processed": 5, "leads": 3, "skipped": 2, "lead_ids": ANY}

async def test_concurrency_cap(db, ...):        # 20 rows, settings.discovery_concurrency=3 -> max in-flight enrich_company == 3

async def test_overlapping_batches_never_double_process(db, ...):   # Review Focus
    a, b = await asyncio.gather(process_next_batch(cid, 10, "vp"), process_next_batch(cid, 10, "vp"))
    assert a["processed"] + b["processed"] == 10 and enrich_calls_by_domain all == 1

async def test_first_campaign_wins_shared_domain(db, ...):
    # same domain pending in campaign 1 (lower pool id) and campaign 2; processing campaign 2 -> skip "in_other_campaign"
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_lead_builder.py`
- [ ] **Step 3: Implement.**
  - Claim with one statement:
    `UPDATE campaign_companies SET status='processing' WHERE id IN (SELECT id FROM campaign_companies WHERE campaign_id=? AND status='pending' ORDER BY tier, confidence DESC, id LIMIT ?) RETURNING *`.
  - Run with `asyncio.Semaphore(settings.discovery_concurrency)` and
    `asyncio.gather`.
  - Before enriching, skip as `in_other_campaign` when another campaign
    has a lead with this domain, or a `pending|processing|done` row for it
    with a lower `id`.
  - `found` = the scraped emails plus the Overture `emails` that end in
    `@{domain}` or `.{domain}`. Verify at most 3 found addresses, in
    order, with `verify_email_smtp(email, timeout=5.0)`. Stop at the first
    one that isn't `invalid`.
  - Person: `people = _extract_people_with_llm(domain, content_summary)`.
    Guess with `enrich_person_email` for `people[0]` only.
  - Insert the lead with `status='enriched'`.
  - Write row status, `skip_reason` and `lead_id`, plus
    `campaigns.total_leads`, as each row finishes. Call
    `refresh_pool_counters` at the end.
- [ ] **Step 4: Run them to see them pass.** `scripts/test.sh tests/test_lead_builder.py`
- [ ] **Step 5: Commit.**
  `git commit -m "feat: parallel lead building with real-emails-only rule"`

---

### Task 7: Campaign API wiring

**Files:**
- Modify: `app/api/campaigns.py`
- Test: `tests/test_campaigns_api.py` (use
  `httpx.AsyncClient(transport=ASGITransport(app=app))`; call the
  background tasks directly)

**Interfaces:**
- Consumes: `refresh_campaign_pool` (Task 5), `process_next_batch` (Task 6).
- Produces:
  - `async def run_discovery_task(campaign_id: int, auto_send: bool = False) -> None`:
    - sets status `discovering`
    - runs `refresh_campaign_pool` if `search_plan` is null
    - runs `process_next_batch(campaign_id, batch_size, value_proposition)`
    - sets status `ready`
    - with `auto_send`, runs `dispatch_campaign_batch(campaign_id, limit=leads_created)`
    - on any exception: status `error`, and `discovery_message` = the
      `PlanError`/`OvertureError` text, or
      `"Discovery failed: <ExceptionType>: <msg>"`
  - `async def run_refresh_task(campaign_id: int) -> None`: status
    `discovering`, then `refresh_campaign_pool`, then `ready` (or `error`
    as above).
  - `POST /api/campaigns/{id}/refresh-search` returns
    `{"id", "status": "discovering"}`. Its 404 matches `/discover`.
  - `CampaignCreate.batch_size: Optional[int] = None`, stored as
    `batch_size or settings.discovery_batch_size`. Clamp it to 1–1000.
  - The autopilot inserts `batch_size = max(1, min(req.leads_per_campaign, 1000))`
    and calls `run_discovery_task(cid, req.auto_send)`.
  - `GET /api/campaigns` items include `search_plan` parsed to a dict (or
    null), plus `pool_total`, `pool_pending` and `discovery_message`.

- [ ] **Step 1: Write the failing tests** in `tests/test_campaigns_api.py`:

```python
async def test_create_runs_search_then_one_batch(...):   # refresh called once, process_next_batch(cid, 50, vp), status 'ready'
async def test_discover_does_not_search_again_when_plan_exists(...)   # refresh not called
async def test_refresh_endpoint_searches_but_creates_no_leads(...)   # process_next_batch not called
async def test_overture_failure_sets_error_and_message(...):          # Review Focus
    # refresh raises OvertureError("HTTP 503 from s3") -> status 'error', discovery_message contains "HTTP 503"
async def test_plan_error_message_shown(...)                      # PlanError text lands in discovery_message
async def test_list_returns_parsed_plan_and_pool_counts(...)
async def test_autopilot_sets_batch_size_from_leads_per_campaign(...)
async def test_batch_size_default_and_clamp(...)                  # None -> 50; 5000 -> 1000
```

- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_campaigns_api.py`
- [ ] **Step 3: Implement the wiring.** Remove the old inline
  lead-building loop from `run_discovery_task`; it lives in
  `lead_builder` now.
- [ ] **Step 4: Run every test, plus the legacy check script.**
  `scripts/test.sh` shows all passed. The legacy script (the Task 1, Step
  5 command) prints `ALL CHECKS PASSED!`.
- [ ] **Step 5: Commit.**
  `git commit -m "feat: wire Overture discovery into campaigns API"`

---

### Task 8: UI, attribution and docs

**Files:**
- Modify: `app/static/index.html`, `README.md`, `.env.example`
- Test: `tests/test_ui_static.py`

**Interfaces:**
- Consumes: the `GET /api/campaigns` fields (Task 7) and
  `POST /api/campaigns/{id}/refresh-search`.
- Produces:
  - In `loadCampaigns()` cards, the spec's block:
    - `Searching: <categories joined ", ">`
    - `In: <area (tier N)> · …`
    - `<pool_total> companies found · <processed> processed (<total_leads> leads, <skipped> skipped) · <pool_pending> in reserve`,
      where processed = `pool_total - pool_pending` and skipped =
      processed - leads
    - `discovery_message` in amber
    - buttons "Find next <batch_size> leads" (existing `discoverCampaign`)
      and "Refresh search" (new `refreshSearch(id)`)
    - a "Searching Overture… (large areas can take a few minutes)" line
      while `discovering`
  - The create form gets `<input type="number" id="camp-batch" min="1" max="1000" placeholder="50">`,
    labelled "Companies per run", and sends `batch_size` when filled.
  - The footer gets the attribution copy from Global Constraints. The
    README gets a "Data sources & attribution" section with the same line
    and the Overture license links, and its feature list is updated: lead
    discovery comes from Overture Maps open data.
  - `.env.example` documents the five new settings with their defaults.

- [ ] **Step 1: Write the failing tests** in `tests/test_ui_static.py`:

```python
def test_footer_attribution(): assert "Business data: Overture Maps Foundation (CDLA-Permissive-2.0 / Apache-2.0 / CC0)" in HTML
def test_refresh_and_batch_controls(): assert "refreshSearch(" in HTML and 'id="camp-batch"' in HTML and "refresh-search" in HTML
def test_inline_js_parses():  # extract inline <script> blocks, write to tmp, run `node --check` if node exists else pytest.skip
```

  `node` is absent in the image, so the last test skips there. Also run
  the check on the host in Step 4.
- [ ] **Step 2: Run them to see them fail.** `scripts/test.sh tests/test_ui_static.py`
- [ ] **Step 3: Implement the UI, README and `.env.example` changes.**
- [ ] **Step 4: Verify.** `scripts/test.sh tests/test_ui_static.py` passes.
  On the host, the inline-JS `node --check` from the earlier session
  prints `inline JS parses OK`.
- [ ] **Step 5: Commit.**
  `git commit -m "feat: show search plan and pool in UI; Overture attribution"`

---

### Task 9: Live verification

**Files:**
- Create: `tests/test_overture_live.py` (marked `live`, and skipped unless
  `OPENEXPLEE_LIVE=1`)

- [ ] **Step 1: Write the live smoke test.** `get_release()` returns a
  `YYYY-MM-DD.N` string. Resolving `AreaSpec("Tampa", "locality", 1, "US-FL")`
  succeeds. `fetch_places(release, ["dental_clinic"], areas, 0.5)` returns
  more than 100 rows in under 60 s.
- [ ] **Step 2: Run it.** `OPENEXPLEE_LIVE=1 scripts/test.sh -m live`
  shows 1 passed.
- [ ] **Step 3: Run end to end in the real app.**
  - `docker compose up -d --build`
  - `POST /api/campaigns` with ICP "Dental clinics and chiropractors in
    Pinellas and Hillsborough County, Florida", `batch_size` 10 and a
    value proposition. This makes about 20 real LLM calls and only SMTP
    handshakes; nothing is sent.
  - Poll `GET /api/campaigns` until `ready`.
  - Expected: `search_plan` has `dental_clinic` and `chiropractic` with two
    tier-1 counties, `pool_total` > 300, `pool_pending` = `pool_total - 10`,
    and every new lead has a non-empty email, none starting `contact@`
    unless that address was found on the site.
  - The UI card shows the plan, the counts and both buttons.
- [ ] **Step 4: Time a nationwide search.** `POST .../refresh-search` on a
  campaign with ICP "Dental clinics in the United States", without
  processing. Record the elapsed time and `pool_total` in the PR
  description. If it's over ~10 minutes, open a follow-up issue rather
  than tuning in this branch.
- [ ] **Step 5: Commit the live test.**
  `git commit -m "test: opt-in live Overture smoke test"`
