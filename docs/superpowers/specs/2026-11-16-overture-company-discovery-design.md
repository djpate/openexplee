# Company discovery from Overture Maps Places

Piece 1 of 3. Piece 2 is contacts (owner names from About/Team pages, the
NPPES health-provider registry, state business registries). Piece 3 is
website-quality scoring. Both are out of scope here.

## Why

OpenExplee finds companies by asking the LLM to name some from memory, then
scraping Bing and DuckDuckGo. From a server, DuckDuckGo returns a bot check
and Bing returns results unrelated to the query, so in practice every lead
comes from the LLM's memory. A real test campaign ("independent bars,
restaurants, escape rooms… in St. Petersburg and Tampa Bay FL") produced 8
leads, all famous venues, one with a founder who died in 2002. Discovery is
also capped at 8 companies (`run_discovery_task(max_companies=8)` in
`app/api/campaigns.py`), with no way to change it.

Explee, the SaaS this project replaces, queries a pre-built company database
instead. Overture Maps Places is an open, monthly-updated database of about
88 million places with name, category, address, websites, emails, phones and
a brand (chain) tag, under permissive licenses (CDLA-Permissive-2.0,
Apache-2.0, CC0 depending on the source). It's a real source of companies
that any OpenExplee user can query for free, with no account.

OpenExplee is a general self-hosted tool. This must work for any user,
whatever their business and target area, not one specific customer.

## Measured facts this design relies on

All measured on 2026-11-16 against release `2026-10-21.0`, from a Docker
container on a home connection:

- Tampa Bay bounding box: 256,910 places. 216,296 are open with confidence
  ≥0.5, 208,117 have a website and 91,625 have an email. 21,577 have a brand
  (chain) tag. After removing chains, closed places and social/platform
  links, about 23,000 local service businesses have their own website.
- Only ~3% of non-chain websites are social or platform links. Some chains
  have no brand tag (Western Union, U-Haul, UPS); dropping any domain used
  by 4+ places catches them. A few directory hosts slip through (e.g.
  `hub.biz`).
- Live search times (one category, "has a website"): Tampa Bay 8 s, Florida
  12 s (11,101 dentists), contiguous US ~127 s (166,928 dentists).
- Area lookup in Overture's divisions theme (`type=division_area`, by
  `names.primary` + `subtype` + `region`) takes ~2 s and resolves ambiguous
  names by state (Hillsborough County FL vs NH; Austin TX vs six others).
  A name can return several rows for the same place. Merge them by taking
  the union of their boxes.
- `basic_category` has ~270 distinct values, small enough to give the LLM
  the whole list. `taxonomy.primary` has ~1,700.
- The latest release can be found by listing
  `s3://overturemaps-us-west-2/release/` (anonymous access, region
  `us-west-2`).

## Goals

- Campaign discovery finds real companies from Overture Places. It no
  longer uses LLM memory or Bing/DuckDuckGo scraping.
- Works for any area size, from one city to the whole US, with no setup step
  and no API key. (Overture is global, so other countries work too.)
- Volume in line with Explee for local business types: thousands of
  companies for a metro area, not 8.
- The user can see what was searched: the business categories, the areas,
  and how many companies were found, processed and held in reserve.
- Every company found is kept per campaign as a pool. Each discovery run
  processes the next batch from the pool, so "find more leads" doesn't
  re-search.
- Each batch is processed in parallel, so hundreds of companies take
  minutes, not hours.

## Non-goals

- New contact sources (NPPES, state registries, paid enrichment) and
  website-quality scoring. Those are pieces 2 and 3. Contact finding and
  email drafting reuse the existing per-company logic unchanged.
- Filtering by employee count. Overture has no size field. "Small business"
  is approximated as "not a chain".
- A new source for business types without a public location (SaaS,
  agencies, remote firms). Overture covers them poorly. For these ICPs the
  existing Serper (Google search) path is used when the user has a Serper
  key. Without one the campaign shows a warning (see "Non-local ICPs").
  Bing/DuckDuckGo scraping and LLM recall are not used.
- Editing the search plan or the ICP in the UI. In v1 the plan is shown
  read-only. To target something else, create a new campaign.

## Anti-goals

- **No invented companies.** Every company must come from Overture. Remove
  the LLM-recall step (`_suggest_domains_via_llm`) and the hard-coded
  `ICP_CURATED_DOMAINS` list from campaign discovery.
- **No chains, directories or social pages as leads.**
- **No silent empty results.** If areas or categories can't be resolved, or
  the search finds nothing, the campaign shows why.
- **Never block the server.** DuckDB calls are synchronous. Run them off the
  event loop (`asyncio.to_thread`). The UI stays responsive during a
  2-minute nationwide search.
- **Don't break existing behavior:** existing campaigns and leads, the
  autopilot (`/api/campaigns/autopilot`), the MCP tools, the follow-up
  scheduler, sending.

## Approaches considered

1. **Live search, saved per campaign (chosen).** Each campaign's search
   queries Overture's public files for that area and those categories. All
   results are saved locally as the campaign's pool. No setup, always the
   current release, and works for both metro and nationwide users. A
   nationwide search takes ~2 minutes, which is fine for a background job.
2. **Import regions up front (rejected).** The user downloads a state or the
   whole US in Settings (~130 MB for Florida, ~2 GB for the US), then
   searches locally. Rejected: it adds a setup step and a monthly refresh,
   and searching outside the imported area silently finds nothing.
3. **A hosted shared database (not considered further).** OpenExplee is
   self-hosted, so there's no server to host it on.

## Design

### Flow of one discovery run

When a campaign is created, or when "Refresh search" is clicked, the
background job does:

1. **Plan.** The LLM turns the ICP text into a search plan (JSON):
   - `categories`: values chosen only from the current release's
     `basic_category` list, which is provided in the prompt.
   - `areas`: each with `name`, `kind` (`locality` | `county` | `region` |
     `country`), `region_code` (e.g. `US-FL`) or `country_code`, and
     `tier` (1 = highest priority). Priority wording in the ICP, like
     "Tampa Bay first, then the rest of Florida", maps to tiers.
   - `is_local_business`: false for ICPs that Overture covers poorly.

   Validate the plan. Drop unknown categories. If the ICP names no place,
   use the `DEFAULT_SEARCH_AREA` setting (default "United States") as one
   tier-1 area, and say so in the campaign. *(Builder's call; the autopilot
   generates place-less ICPs.)* If no valid categories are left and the ICP
   is local, stop with an error that says so.
2. **Resolve areas** through the divisions theme to bounding boxes. Report
   unresolved names in the campaign. Region- and country-level areas also
   need a post-filter on the place's address (see Builder's calls).
3. **Fetch** places in one query per run against the current release:
   - point inside any area box
   - basic category in the plan
   - `operating_status` not `permanently_closed`
   - `confidence` ≥ `OVERTURE_MIN_CONFIDENCE` (default 0.5)
   - at least one website

   Columns needed: Overture id, name, category, websites, emails, phones,
   brand, confidence, address (locality, region, country, freeform), and
   coordinates. Each place's tier is the best (lowest) tier among the areas
   that contain it.
4. **Clean:**
   - Normalize the domain from the first website.
   - Drop places with a brand tag.
   - Drop social, platform and directory hosts (Facebook, Instagram,
     LinkedIn, Yelp, linktr.ee, Google, wixsite, godaddysites, square.site,
     hub.biz, …).
   - Drop domains used by 4 or more places in the fetched set.
   - Drop domains on a franchise blocklist (seed it from known national
     chains: Servpro, Aspen Dental, Anytime Fitness, Great Clips, Jiffy Lube,
     Massage Envy, State Farm and Allstate agent pages, and similar).
   - Drop domains on the suppression list.
   - Drop domains already held by another campaign, as a lead or a
     pending pool row (see cross-campaign dedupe below).
   - Dedupe by domain, keeping the highest confidence.
5. **Save the pool** to `campaign_companies`, ordered by tier, then by
   confidence (descending).
6. **Process a batch:**
   - Take the next `batch_size` pending pool rows and enrich them in
     parallel, at most `DISCOVERY_CONCURRENCY` at a time.
   - Per company, reuse the current logic:
     - scrape the website
     - keep emails on the company's own domain, adding Overture's emails on
       that domain
     - extract people with the LLM
     - guess and verify their email
     - draft the cold email
   - **Lead rule: real emails only** (decided). A company becomes a lead
     only with one of these:
     - an address found on its site or in Overture, on its own domain, that
       doesn't verify as `invalid`. A business publishes its own address,
       so `risky` and `catch_all` are accepted for found addresses.
     - a guessed person address (first.last@ etc.) that verifies as
       `deliverable`/`verified`.

     The `contact@`/`hello@` fallback is removed. Otherwise the pool row is
     `skipped` with a reason:
     - `site_unreachable`: the site didn't load, so the business may be
       closed
     - `no_email`
     - `email_invalid`
     - `in_other_campaign`

     Prefer a named person's verified address over a found role address.
     Only draft the cold email (an LLM call) for companies that become
     leads.
   - Insert a lead, and mark the pool row `done` with its `lead_id`, or
     `skipped` with its reason.
   - Update the campaign's counters as rows finish, so the UI shows
     progress. A batch is N pool rows, not N leads. The UI reports both
     ("150 processed: 112 leads, 38 skipped").

### Non-local ICPs (decided: web search fallback)

When the plan says `is_local_business: false`:
- Still run the Overture search, if any categories matched.
- If a Serper key is set, also have the LLM write search queries. It writes
  queries, never company names. Use the existing `_search_queries_for_icp`
  and run them through `search_serper`. Feed the results into the same
  pool (`source='serper'`), through the same cleaning rules plus the
  existing `NON_COMPANY_HOSTS` filter.
- Without a Serper key, `discovery_message` says: "This target isn't a
  local business type. Open map data covers it poorly. Add a Serper key in
  Settings for web search results."

Bing/DuckDuckGo are never used. Serper results have no category, address
or tier, so they get tier 1 and empty location fields. The number of
queries (Serper bills per query) is the builder's call. Keep it modest and
configurable.

"Find more leads" (the existing `POST /api/campaigns/{id}/discover`) runs
step 6 only, on the next batch. It searches again (steps 1–5) only if the
campaign has no plan yet. Campaign creation runs steps 1–6.

"Refresh search" (a new endpoint) runs steps 2–5 only. It reuses the
stored plan (no new LLM call, so the categories don't drift), fetches
from the latest release, and adds companies not already in the pool. It
doesn't process a batch, create leads or send anything. *(Builder's call.)*
There is no ICP editing in this piece. To target something different, the
user creates a new campaign, so stale pending rows can't arise.

**Cross-campaign dedupe** *(builder's call)*:
- A domain belongs to the first campaign that holds it, either as a lead
  or as a pending/processing pool row. Cleaning (step 4) drops domains held
  by any other campaign.
- Step 6 re-checks each company just before processing it, because
  campaigns created together by the autopilot search at the same time. A
  company already held elsewhere is marked `skipped` with reason
  `in_other_campaign`.
- MCP `search_companies` is read-only, so it doesn't apply this rule.

### Data model

All changes are additive, through the existing `CREATE TABLE IF NOT EXISTS`
and `ALTER TABLE ADD COLUMN` migration list in `app/database.py`.

- **`campaigns`:** add
  - `search_plan` TEXT (the validated plan as JSON, including the resolved
    areas)
  - `overture_release` TEXT
  - `pool_total` INTEGER
  - `pool_pending` INTEGER
  - `batch_size` INTEGER DEFAULT 50
  - `discovery_message` TEXT (the latest warning or error, readable by the
    user)
- **New table `campaign_companies`:**
  - id; `campaign_id` (FK, cascade delete); `source` ('overture'); `source_id`
  - name; category; domain; `website_url`
  - emails (JSON); phones (JSON)
  - city; region; country; lat; lon
  - tier; confidence
  - status (`pending` | `processing` | `done` | `skipped`); `skip_reason`
  - `lead_id`; `created_at`
  - `UNIQUE(campaign_id, domain)`
- **`leads`:** add `phone` TEXT and `company_id` INTEGER, which points to
  the pool row.
- **On startup:** reset `campaign_companies` rows stuck in `processing` to
  `pending`, the same way the existing reset handles `discovering`
  campaigns.

### Settings (env vars, defaults in `app/config.py`)

- `OVERTURE_RELEASE`: blank means the latest release, found by listing. Cache
  the latest release for 24 h.
- `OVERTURE_MIN_CONFIDENCE`: 0.5
- `DISCOVERY_BATCH_SIZE`: 50, the default for new campaigns
- `DISCOVERY_CONCURRENCY`: 8
- `DEFAULT_SEARCH_AREA`: "United States", used when the ICP names no place

The `basic_category` list is read once per release and cached locally, so a
search doesn't wait for it.

### UI

On the campaign card and detail view, replace the bare lead count with the
plan and the pool status:

```
Searching: dental_clinic, chiropractic, physical_therapy
In: Pinellas County, FL · Hillsborough County, FL (tier 1) · Florida (tier 2)
2,431 companies found · 150 processed (112 leads, 38 skipped) · 2,281 in reserve
[ Find next 50 leads ]   [ Refresh search ]
⚠ Open map data covers this business type poorly. Expect few results.
```

- While the search runs: "Searching Overture… (large areas can take a few
  minutes)".
- Errors appear in the same spot, from `discovery_message`.
- The campaign create form gets an optional "Companies per run" field
  (default 50).
- Add an attribution line in the page footer: "Business data: Overture
  Maps Foundation (CDLA-Permissive-2.0 / Apache-2.0 / CC0)". The same
  attribution goes in the README.

### Other callers

- **Autopilot:** passes `leads_per_campaign` as the campaign's batch size.
- **MCP `search_companies`:** keeps its name and input (`definition`,
  `limit`). It runs plan → resolve → fetch → clean and returns up to
  `limit` companies (name, domain, website, category, city, region, emails,
  phones) without enrichment. Update its description, which currently says
  "via web search and live scraping".
- **MCP `search_people_by_domains` and `search_companies_by_domains`:**
  unchanged (piece 2 revisits people search).
- **Bing/DuckDuckGo search functions:** keep them for those people tools,
  but campaign discovery no longer calls them. Serper is called only for
  non-local ICPs (above).

### Packaging

- Add `duckdb` to `requirements.txt`.
- Preinstall the DuckDB `httpfs` extension when the Docker image is built,
  so the first search doesn't download it at runtime.
- No `spatial` extension: filtering uses the `bbox` columns.

### Testing

- **Offline unit tests, no network.** Point the engine at local parquet
  fixtures built in the tests (small hand-made places and division_area
  tables) through a configurable source path. Cover:
  - plan validation: unknown categories dropped; an empty plan gives an
    error
  - area resolution, including duplicate rows for the same place and
    ambiguous names
  - fetch filters
  - every cleaning rule
  - tier ordering
  - cross-campaign dedupe
  - batch processing with the scraper, LLM and SMTP mocked
  - the concurrency cap
  - pool status transitions and the startup reset
  - "Find more" not searching again
  - the lead rule: a found address is accepted when risky/catch-all; a
    guessed address is accepted only when deliverable; no `contact@`
    fallback; each skip reason is recorded
  - "Refresh search" adds only new companies and creates no leads
  - a place-less ICP falls back to `DEFAULT_SEARCH_AREA`
  - non-local ICPs: Serper is called when a key is set, the warning is shown
    when it isn't, and Bing/DuckDuckGo are never called
- **One live smoke test, opt-in through an env var:** "dentists in Tampa,
  FL" returns more than 100 companies in under 60 s.
- The existing `tests/test_cloudflare_and_settings.py` must still pass.

## Left to the builder

- The plan prompt wording, and whether to use `taxonomy.primary` alongside
  `basic_category` for precision. Start with `basic_category`.
- How to filter region- and country-level areas. A country box for the US
  is useless: Alaska's Aleutians cross the antimeridian, so the box spans
  every longitude. Use per-area boxes plus an address
  `region`/`country` post-filter, after checking the exact format of
  Overture's address `region` values.
- The concurrency mechanism (e.g. `asyncio.Semaphore`) and how progress is
  written.
- The exact contents of the platform/directory host list and the franchise
  blocklist.
- UI styling.
