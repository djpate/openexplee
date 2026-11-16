"""Turn a plain-English ICP into a validated Overture search plan."""
import json
import re
from dataclasses import asdict, dataclass, field

from app.config import settings
from app.engine.llm import call_llm_json
from app.engine.overture import AreaSpec

VALID_KINDS = {"locality", "county", "region", "country"}


class PlanError(Exception):
    pass


@dataclass
class SearchPlan:
    categories: list[str]
    areas: list[AreaSpec]
    is_local_business: bool
    messages: list[str] = field(default_factory=list)


def plan_to_json(plan: SearchPlan) -> str:
    return json.dumps(asdict(plan))


def plan_from_json(s: str) -> SearchPlan:
    d = json.loads(s)
    return SearchPlan(
        categories=d["categories"],
        areas=[AreaSpec(**a) for a in d["areas"]],
        is_local_business=d["is_local_business"],
        messages=d.get("messages", []),
    )


def _snake(s: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(s).strip().lower())


def _build_prompt(icp_prompt: str, categories: list[str]) -> str:
    return f"""Convert this target customer description into a search plan over Overture Maps Places.

Target customer description:
{icp_prompt}

Allowed Overture categories (use ONLY these exact values):
{", ".join(categories)}

Return JSON only, in this shape:
{{"categories": ["..."],
  "is_local_business": true,
  "areas": [{{"name": "...", "kind": "locality|county|region|country", "region_code": "US-FL", "country_code": "US", "tier": 1}}]}}

Rules:
- Pick the categories that best match the kinds of business described.
- Use official place names (e.g. "Pinellas County", "Florida").
- Every region, county and locality area MUST include an ISO 3166-2 region_code (e.g. US-FL for Florida). Country areas MUST include an ISO country code in country_code (e.g. US).
- Map priority wording to tiers: "first"/"primarily" = 1, "then"/"next" = 2, later fallbacks = 3. Areas with no stated priority are tier 1.
- Set is_local_business to false for businesses without a public storefront or location (e.g. software companies); true otherwise.
- If no place is mentioned, return an empty areas list."""


def _parse_area(a) -> tuple[AreaSpec | None, str | None]:
    if not isinstance(a, dict) or not a.get("name"):
        return None, None
    kind = _snake(a.get("kind", ""))
    if kind not in VALID_KINDS:
        return None, None
    name = str(a["name"]).strip()
    region_code = (a.get("region_code") or None)
    if kind == "region" and not region_code:
        return None, (f"Dropped area '{name}': couldn't determine its region code, "
                      "so it can't be searched. Try naming it more specifically.")
    try:
        tier = int(a.get("tier") or 1)
    except (TypeError, ValueError):
        tier = 1
    return AreaSpec(name, kind, tier, region_code, a.get("country_code") or None), None


async def build_search_plan(icp_prompt: str, categories: list[str]) -> SearchPlan:
    data = await call_llm_json(_build_prompt(icp_prompt, categories))
    if not isinstance(data, dict) or not isinstance(data.get("areas", []), list) \
            or not isinstance(data.get("categories", []), list):
        raise PlanError("Couldn't build a search plan: the AI provider returned nothing usable.")
    if "categories" not in data and "areas" not in data:
        raise PlanError("Couldn't build a search plan: the AI provider returned nothing usable.")

    allowed = set(categories)
    cats: list[str] = []
    for c in data.get("categories", []):
        c = _snake(c)
        if c in allowed and c not in cats:
            cats.append(c)
    is_local = bool(data.get("is_local_business", True))
    if is_local and not cats:
        raise PlanError("None of the business types in the target matched Overture's categories.")

    messages: list[str] = []
    areas: list[AreaSpec] = []
    for a in data.get("areas", []):
        spec, msg = _parse_area(a)
        if spec:
            areas.append(spec)
        if msg:
            messages.append(msg)
    if not areas:
        default = settings.default_search_area
        areas = [AreaSpec(default, "country", 1)]
        messages.append(f"No search area was found in the target, so {default} will be searched.")
    return SearchPlan(cats, areas, is_local, messages)
