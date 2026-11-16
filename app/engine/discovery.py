import asyncio
import functools
import httpx
import json
import re
import base64
from urllib.parse import urlparse, quote_plus, parse_qs
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Set
from app.config import settings
from app.database import get_db_connection
from app.engine import company_pool
from app.engine.overture import fetch_places, get_release, load_categories, resolve_areas
from app.engine.search_plan import PlanError, SearchPlan, build_search_plan, plan_from_json, plan_to_json
from app.engine.scraper import scrape_company_website
from app.engine.verifier import enrich_person_email

DUCKDUCKGO_LITE_URL = "https://html.duckduckgo.com/html/"

PERSON_TITLE_REGEX = re.compile(
    r"\b([A-Z][a-z]{1,20}\s+[A-Z][a-z]{1,20})\s*[,|\-–—]\s*"
    r"(CEO|CTO|COO|CFO|Founder|Co-Founder|Head of [A-Za-z]+|VP [A-Za-z]+|Director of [A-Za-z]+)"
)


async def _get_suppressed_domains() -> Set[str]:
    """Thin alias kept for existing call sites; logic lives in company_pool."""
    from app.engine.company_pool import suppressed_domains
    return await suppressed_domains()


async def _extract_people_with_llm(domain: str, text: str, job_titles: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Uses Claude CLI / Thor Ollama / LLM or regex heuristics to extract decision-makers."""
    if text:
        from app.engine.llm import call_llm_json

        titles_hint = f"Target roles: {', '.join(job_titles)}." if job_titles else "Focus on Founders, CEO, CTO, VP/Head of Engineering, AI, or Sales."
        prompt = (
            f"Extract real person names and job titles associated with domain {domain} from this text. "
            f"{titles_hint}\nText:\n{text[:3000]}\n\n"
            'Return ONLY a valid JSON array of objects: [{"first_name": "...", "last_name": "...", "job_title": "..."}]. '
            "Return [] if no real human names are present."
        )
        parsed = await call_llm_json(prompt)
        if isinstance(parsed, list):
            extracted = [p for p in parsed if isinstance(p, dict) and p.get("first_name") and p.get("last_name")]
            if extracted:
                return extracted[:5]

    people = []
    seen = set()
    for match in PERSON_TITLE_REGEX.finditer(text or ""):
        full_name, title = match.group(1).strip(), match.group(2).strip()
        parts = full_name.split()
        if len(parts) == 2 and full_name not in seen:
            seen.add(full_name)
            people.append({"first_name": parts[0], "last_name": parts[1], "job_title": title})
    return people[:5]


async def search_duckduckgo(query: str, max_results: int = 10) -> List[Dict[str, str]]:
    """
    Free, zero-credential web search via DuckDuckGo HTML.
    Extracts organic search results (title, snippet, URL).
    """
    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://html.duckduckgo.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            resp = await client.post(DUCKDUCKGO_LITE_URL, data={"q": query, "b": ""})
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                links = soup.find_all("a", class_="result__snippet")
                titles = soup.find_all("a", class_="result__url")
                
                # Extract from result__url and standard links
                skip_hosts = {"duckduckgo.com", "bing.com", "doubleclick.net", "google.com", "yahoo.com", "youtube.com", "facebook.com", "twitter.com", "x.com"}
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    real_url = None
                    if "uddg=" in href:
                        raw = href.split("uddg=")[-1].split("&")[0]
                        from urllib.parse import unquote
                        real_url = unquote(raw)
                    elif href.startswith("http://") or href.startswith("https://"):
                        real_url = href

                    if real_url:
                        parsed = urlparse(real_url)
                        domain = parsed.netloc.replace("www.", "").lower()
                        if domain and not any(skip in domain for skip in skip_hosts) and not domain.endswith(".edu") and not domain.endswith(".gov"):
                            title = a.get_text(strip=True) or domain
                            if not any(r["domain"] == domain for r in results):
                                results.append({
                                    "title": title[:100],
                                    "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                                    "domain": domain
                                })
                    if len(results) >= max_results:
                        break
    except Exception as e:
        print(f"[Discovery] DuckDuckGo search error: {e}")

    return results


async def search_bing(query: str, max_results: int = 10) -> List[Dict[str, str]]:
    """
    Organic web search fallback via Bing HTML with redirect decoding.
    """
    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    encoded = quote_plus(query)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(f"https://www.bing.com/search?q={encoded}")
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for li in soup.find_all("li", class_="b_algo"):
                    h2 = li.find("h2")
                    a = h2.find("a") if h2 else None
                    if not a or not a.get("href"):
                        continue
                    href = a["href"]
                    title = a.get_text(strip=True)
                    real_url = href
                    if "/ck/a?" in href:
                        parsed_q = parse_qs(urlparse(href).query)
                        u_val = parsed_q.get("u", [""])[0]
                        if u_val and len(u_val) > 2:
                            try:
                                b64 = u_val[2:] + "=" * (-len(u_val[2:]) % 4)
                                decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                                if decoded.startswith("http"):
                                    real_url = decoded
                            except Exception:
                                pass
                    parsed_dest = urlparse(real_url)
                    domain = parsed_dest.netloc.replace("www.", "").lower()
                    if domain and "bing.com" not in domain:
                        results.append({
                            "title": title[:100],
                            "url": f"{parsed_dest.scheme}://{parsed_dest.netloc}{parsed_dest.path}",
                            "domain": domain
                        })
                    if len(results) >= max_results:
                        break
    except Exception as e:
        print(f"[Discovery] Bing search error: {e}")

    return results


async def search_serper(query: str, api_key: str, max_results: int = 15) -> List[Dict[str, str]]:
    """Google Search via Serper API if user has configured SERPER_API_KEY."""
    results = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results}
            )
            if resp.status_code == 200:
                data = resp.json()
                for org in data.get("organic", []):
                    link = org.get("link", "")
                    parsed = urlparse(link)
                    if parsed.netloc:
                        results.append({
                            "title": org.get("title", ""),
                            "url": link,
                            "domain": parsed.netloc.replace("www.", ""),
                            "snippet": org.get("snippet", "")
                        })
    except Exception as e:
        print(f"[Discovery] Serper search error: {e}")
    return results


async def search_companies_by_domains(domains: List[str]) -> List[Dict[str, Any]]:
    """Bulk-enriches company profiles by domain name (replaces Explee's search_companies_by_domains)."""
    suppressed = await _get_suppressed_domains()
    companies = []
    for raw_domain in domains:
        domain = raw_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/ ")
        if not domain or domain in suppressed:
            continue
        intel = await scrape_company_website(f"https://{domain}")
        companies.append({
            "company_name": (intel.get("title") or domain).split("-")[0].split("|")[0].strip(),
            "company_domain": intel["domain"],
            "website_url": intel["url"],
            "description": intel.get("description", ""),
            "summary": intel.get("content_summary", "")[:1000],
            "found_emails": intel.get("found_emails", []),
        })
    return companies


async def search_people_by_domains(
    domains: List[str], job_titles: Optional[List[str]] = None, people_per_company: int = 2
) -> List[Dict[str, Any]]:
    """Finds real decision-makers at target domains and enriches their work emails ($0)."""
    suppressed = await _get_suppressed_domains()
    contacts = []
    role_query = " OR ".join(job_titles[:3]) if job_titles else "CEO OR Founder OR CTO"

    for raw_domain in domains:
        domain = raw_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/ ")
        if not domain or domain in suppressed:
            continue

        intel = await scrape_company_website(f"https://{domain}")
        search_hits = await (
            search_serper(f'"{domain}" ({role_query})', settings.serper_api_key, 5)
            if settings.serper_api_key
            else search_duckduckgo(f'"{domain}" ({role_query})', 5)
        )
        combined_text = intel.get("content_summary", "") + "\n" + "\n".join(
            f"{h.get('title', '')} {h.get('snippet', '')}" for h in search_hits
        )

        people = await _extract_people_with_llm(domain, combined_text, job_titles)
        for person in people[:people_per_company]:
            enriched = await enrich_person_email(person["first_name"], person["last_name"], domain)
            contacts.append({
                "company_domain": domain,
                "first_name": person["first_name"],
                "last_name": person["last_name"],
                "contact_name": f"{person['first_name']} {person['last_name']}",
                "job_title": person.get("job_title", "Decision Maker"),
                "email": enriched.get("email"),
                "verification_status": enriched.get("status", "unverified"),
                "verification_details": enriched.get("details", ""),
            })
    return contacts


# Hosts that list or write about companies. A lead must be the company's own site.
NON_COMPANY_HOSTS = (
    "linkedin.", "indeed.", "glassdoor.", "ziprecruiter.", "builtin.", "wellfound.", "angel.co", "startup.jobs",
    "joinhandshake.", "monster.", "simplyhired.", "lever.co", "greenhouse.io", "ashbyhq.", "workable.",
    "ycombinator.", "crunchbase.", "g2.com", "capterra.", "producthunt.", "zoominfo.", "getlatka.", "clutch.co",
    "forbes.", "techcrunch.", "medium.", "substack.", "reddit.", "quora.", "wikipedia.", "github.",
    "nytimes.", "wsj.", "bloomberg.", "businessinsider.", "inc.com", "entrepreneur.", "cnbc.",
    "google.", "bing.", "yahoo.", "duckduckgo.", "msn.", "ibm.", "microsoft.", "apple.", "amazon.",
    "merriam-webster.", "cambridge.", "thesaurus.", "britannica.", ".edu", ".gov", ".ac.uk",
)


async def _search_queries_for_icp(icp_prompt: str) -> List[str]:
    """Turns a long plain-English ICP into short web searches that return company homepages.
    A whole ICP paragraph as one query returns list articles and job boards, not companies."""
    from app.engine.llm import call_llm_json

    parsed = await call_llm_json(
        "Write 4 short web search queries (3 to 7 words each) that return the homepages of companies "
        f"matching this target customer description:\n{icp_prompt}\n\n"
        "Each query names one kind of company or product, not people and not the word 'companies'. "
        'Return ONLY a JSON array of strings, for example ["ai code review tool startup"].'
    )
    queries = [q.strip() for q in parsed if isinstance(q, str) and q.strip()][: settings.serper_max_queries] if isinstance(parsed, list) else []
    # No LLM answer: the first words of the ICP still beat the whole paragraph.
    return queries or [" ".join(icp_prompt.split()[:10])]


NO_SERPER_MESSAGE = (
    "This target isn't a local business type. Open map data covers it poorly. "
    "Add a Serper key in Settings for web search results."
)


async def search_web_candidates(icp_prompt: str) -> List[Dict[str, Any]]:
    """Serper results in the fetch_places shape. Empty when no Serper key is set."""
    if not settings.serper_api_key:
        return []
    out = []
    seen: Set[str] = set()
    for query in (await _search_queries_for_icp(icp_prompt))[: settings.serper_max_queries]:
        for r in await search_serper(query, settings.serper_api_key):
            domain = (r.get("domain") or "").lower()
            if not domain or any(skip in domain for skip in NON_COMPANY_HOSTS):
                continue
            # One hit per site: repeated hits on one domain are not a chain (keep the first).
            key = company_pool.normalize_domain(r.get("url")) or domain
            if key in seen:
                continue
            seen.add(key)
            title = (r.get("title") or "").split(" - ")[0].split("|")[0].strip()
            out.append({
                "source_id": r["url"], "name": title or domain,
                "category": "", "website_url": r["url"], "emails": [], "phones": [], "brand": None,
                "confidence": 0.5, "city": "", "region": "", "country": "", "lat": None, "lon": None, "tier": 1,
            })
    return out


async def _search_parts(icp_prompt: str, plan: Optional[SearchPlan], on_plan=None):
    """Returns plan, overture places, web places, messages, release (uncleaned).
    on_plan(plan, messages) is awaited once the plan is built, validated and its areas
    resolved, before the (slow) place fetch and web search."""
    release = await asyncio.to_thread(get_release)
    if plan is None:
        cats = await asyncio.to_thread(load_categories, release)
        plan = await build_search_plan(icp_prompt, cats)
    messages = list(plan.messages)
    places: List[Dict[str, Any]] = []
    web: List[Dict[str, Any]] = []
    areas = []
    if plan.categories:
        notes: List[str] = []
        areas, _ = await asyncio.to_thread(resolve_areas, plan.areas, release, notes)
        messages += notes
        if plan.areas and not areas:
            raise PlanError("None of the places in the target could be used. " + " ".join(notes))
    if on_plan is not None:
        await on_plan(plan, messages)
    if plan.categories:
        places = await asyncio.to_thread(
            fetch_places, release, plan.categories, areas, settings.overture_min_confidence, None, messages
        )
    if not plan.is_local_business:
        if settings.serper_api_key:
            web = await search_web_candidates(icp_prompt)
        else:
            messages.append(NO_SERPER_MESSAGE)
    return plan, places, web, messages, release


async def search_companies_for_icp(icp_prompt: str, plan: Optional[SearchPlan] = None):
    """Returns (plan, cleaned candidates, user-facing messages, release)."""
    plan, places, web, messages, release = await _search_parts(icp_prompt, plan)
    suppressed = await company_pool.suppressed_domains()
    cands = company_pool.clean_candidates(places + web, set(), suppressed)
    return plan, cands, messages, release


async def _save_search_state(campaign_id: int, plan: SearchPlan, messages: List[str],
                             release: Optional[str] = None) -> None:
    """Store the plan and messages; the release only once a fetch has completed, so a
    plan without a release means the search never finished (run_discovery_task resumes it)."""
    sets, params = "search_plan = ?, discovery_message = ?", [plan_to_json(plan), "\n".join(messages) or None]
    if release:
        sets += ", overture_release = ?"
        params.append(release)
    conn = await get_db_connection()
    try:
        await conn.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*params, campaign_id))
        await conn.commit()
    finally:
        await conn.close()


async def refresh_campaign_pool(campaign_id: int) -> Dict[str, Any]:
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT icp_prompt, search_plan FROM campaigns WHERE id = ?", (campaign_id,))
        row = await cur.fetchone()
    finally:
        await conn.close()
    if not row:
        raise ValueError(f"campaign {campaign_id} not found")
    plan = plan_from_json(row["search_plan"]) if row["search_plan"] else None
    # Store the plan before the fetch: the card shows it during a long first search,
    # and a retry after a fetch failure reuses it.
    plan, places, web, messages, release = await _search_parts(
        row["icp_prompt"], plan, functools.partial(_save_search_state, campaign_id))
    suppressed = await company_pool.suppressed_domains()
    held = await company_pool.held_domains(campaign_id)
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT company_domain FROM leads WHERE campaign_id = ?", (campaign_id,))
        own = {company_pool.normalize_domain(r["company_domain"]) for r in await cur.fetchall()}
    finally:
        await conn.close()
    held = held | (own - {""})
    inserted = 0
    for source, found in (("overture", places), ("serper", web)):
        if found:
            cleaned = company_pool.clean_candidates(found, held, suppressed)
            inserted += await company_pool.save_pool(campaign_id, cleaned, source)
    await _save_search_state(campaign_id, plan, messages, release)
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT pool_total FROM campaigns WHERE id = ?", (campaign_id,))
        total = (await cur.fetchone())["pool_total"]
    finally:
        await conn.close()
    return {"inserted": inserted, "pool_total": total, "messages": messages}
