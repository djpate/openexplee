"""Overture Maps data layer: release lookup, category list, area resolution, place fetch.

All functions are synchronous (DuckDB); async callers wrap them in asyncio.to_thread.
"""
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from app.config import settings

S3_BASE = "s3://overturemaps-us-west-2/release"
RELEASE_TTL = 24 * 3600
WIDE_BOX_DEGREES = 180.0  # a box wider than this can't prune by longitude
SUBTYPES = {
    "locality": ("locality", "localadmin"),
    "county": ("county",),
    "region": ("region",),
    "country": ("country",),
}

_release_cache: tuple[float, str] | None = None


class OvertureError(Exception):
    pass


@dataclass
class AreaSpec:
    name: str
    kind: str  # locality | county | region | country
    tier: int = 1
    region_code: str | None = None  # e.g. "US-FL"
    country_code: str | None = None


@dataclass
class ResolvedArea:
    name: str
    kind: str
    tier: int
    boxes: list[tuple[float, float, float, float]]  # (xmin, ymin, xmax, ymax)
    region_values: set[str] | None  # address.region values allowed anywhere in the area
    country_code: str | None
    # Optional per-box address.region filter, parallel to boxes (None entry = no extra filter).
    box_regions: list[set[str] | None] | None = None


def _places_glob(release: str) -> str:
    return settings.overture_places_source or f"{S3_BASE}/{release}/theme=places/type=place/*"


def _divisions_glob(release: str) -> str:
    return settings.overture_divisions_source or f"{S3_BASE}/{release}/theme=divisions/type=division_area/*"


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _connect(*sources: str):
    con = duckdb.connect()
    if any(s.startswith("s3://") for s in sources):
        con.execute("LOAD httpfs")
        con.execute("SET s3_region='us-west-2'")
    return con


def get_release() -> str:
    global _release_cache
    if settings.overture_release:
        return settings.overture_release
    if settings.overture_places_source:
        return "local"
    if _release_cache and time.time() - _release_cache[0] < RELEASE_TTL:
        return _release_cache[1]
    try:
        con = _connect(S3_BASE)
        try:
            row = con.execute(
                "SELECT max(regexp_extract(file, 'release/([^/]+)/', 1)) "
                f"FROM glob('{S3_BASE}/*/theme=places/type=place/*')"
            ).fetchone()
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    if not row or not row[0]:
        raise OvertureError("No Overture release found")
    _release_cache = (time.time(), row[0])
    return row[0]


def load_categories(release: str) -> list[str]:
    cache = Path(settings.database_path).parent / "overture" / release / "categories.json"
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    src = _places_glob(release)
    try:
        con = _connect(src)
        try:
            rows = con.execute(
                f"SELECT DISTINCT basic_category FROM read_parquet({_lit(src)}, hive_partitioning=1) "
                "WHERE basic_category IS NOT NULL ORDER BY 1"
            ).fetchall()
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    cats = [r[0] for r in rows]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(cats))
    return cats


def _name_variant(name: str) -> str | None:
    m = re.match(r"^(st\.?|saint)\s+(.+)$", name.strip(), re.I)
    if not m:
        return None
    rest = m.group(2)
    return f"Saint {rest}" if m.group(1).lower().startswith("st") else f"St. {rest}"


def _lookup(con, src: str, name: str, subtypes, region_code, country_code):
    sql = (
        "SELECT bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax, country, region, names.\"primary\" "
        f"FROM read_parquet({_lit(src)}, hive_partitioning=1) "
        f"WHERE lower(names.\"primary\") = lower(?) AND subtype IN ({','.join('?' * len(subtypes))})"
    )
    params = [name, *subtypes]
    if region_code:
        sql += " AND region = ?"
        params.append(region_code)
    if country_code:
        sql += " AND country = ?"
        params.append(country_code)
    rows = con.execute(sql, params).fetchall()
    if not rows:
        alt = _name_variant(name)
        if alt:
            params[0] = alt
            rows = con.execute(sql, params).fetchall()
    return rows


def _union(rows) -> tuple[float, float, float, float]:
    return (min(r[0] for r in rows), min(r[1] for r in rows),
            max(r[2] for r in rows), max(r[3] for r in rows))


def _region_suffix(code: str | None) -> str | None:
    """"US-FL" -> "FL" (Overture addresses mostly use the bare code)."""
    if not code:
        return None
    return code.split("-", 1)[1] if "-" in code else code


def _region_values(codes, names) -> set[str]:
    return {v for v in (*(_region_suffix(c) for c in codes), *names) if v}


def _country_boxes(con, src: str, country: str):
    """One union box per distinct region code of a country, plus that region's filter
    when the box is too wide to prune (antimeridian-crossing, e.g. Alaska)."""
    rows = con.execute(
        "SELECT region, names.\"primary\", bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax "
        f"FROM read_parquet({_lit(src)}, hive_partitioning=1) "
        "WHERE subtype = 'region' AND country = ?", [country]).fetchall()
    grouped: dict[str, list] = {}
    loose: set[tuple] = set()
    for code, name, *box in rows:
        if code:
            grouped.setdefault(code, []).append((tuple(box), name))
        else:
            loose.add(tuple(box))
    out: list[tuple[tuple, set[str] | None]] = [(b, None) for b in loose]
    for code, items in grouped.items():
        box = _union([b for b, _ in items])
        wide = box[2] - box[0] > WIDE_BOX_DEGREES
        out.append((box, _region_values([code], [n for _, n in items]) if wide else None))
    out.sort(key=lambda o: o[0])
    return [b for b, _ in out], [r for _, r in out]


def _ambiguity_hint(kind: str) -> str:
    return "Add the country." if kind in ("region", "country") else "Add the state."


def resolve_areas(specs: list[AreaSpec], release: str,
                  messages: list[str] | None = None) -> tuple[list[ResolvedArea], list[str]]:
    """Resolve area specs to bounding boxes. Returns (areas, unresolved names).
    Names that match nothing or match divisions in more than one region/country
    (ambiguous) are unresolved; user-facing notes about them go to `messages`."""
    src = _divisions_glob(release)
    areas: list[ResolvedArea] = []
    missing: list[str] = []
    not_found: list[str] = []
    notes: list[str] = []
    try:
        con = _connect(src)
        try:
            for s in specs:
                rows = _lookup(con, src, s.name, SUBTYPES.get(s.kind, (s.kind,)),
                               s.region_code, s.country_code)
                if not rows:
                    missing.append(s.name)
                    not_found.append(s.name)
                    continue
                places = sorted({(r[4] or "", r[5] or "") for r in rows})
                if len(places) > 1:
                    missing.append(s.name)
                    codes = ", ".join(region or country for country, region in places)
                    notes.append(f"{s.name} is ambiguous ({codes}). {_ambiguity_hint(s.kind)}")
                    continue
                country = rows[0][4] or s.country_code
                region_values = None
                box_regions = None
                if s.kind == "country":
                    boxes, box_regions = _country_boxes(con, src, country)
                    if not boxes:
                        boxes, box_regions = [_union(rows)], None
                elif s.kind == "region":
                    region_values = _region_values([r[5] for r in rows] + [s.region_code],
                                                   [s.name] + [r[6] for r in rows])
                    boxes = [_union(rows)]
                else:
                    boxes = [_union(rows)]
                areas.append(ResolvedArea(s.name, s.kind, s.tier, boxes, region_values, country, box_regions))
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    if messages is not None:
        if not_found:
            messages.append("Couldn't find these places in the map data: " + ", ".join(not_found))
        messages.extend(notes)
    return areas, missing


_PLACE_COLUMNS = (
    "id, names.\"primary\" AS name, basic_category, websites[1] AS website, emails, phones, "
    "brand.names.\"primary\" AS brand, confidence, addresses[1].locality AS city, "
    "addresses[1].region AS region, addresses[1].country AS country, bbox.ymin AS lat, bbox.xmin AS lon"
)


def _region_filter(values: set[str]) -> tuple[str, list[str]]:
    vals = sorted({v.upper() for v in values})
    return (f"(upper(addresses[1].region) IN ({','.join('?' * len(vals))}) "
            "OR addresses[1].region IS NULL OR addresses[1].region = '')", vals)


def _cap_message(cap: int) -> str:
    return (f"More than {cap:,} businesses matched. Kept the {cap:,} best-ranked. "
            "Narrow the area or business types to see others.")


def fetch_places(release: str, categories: list[str], areas: list[ResolvedArea],
                 min_confidence: float, max_places: int | None = None,
                 messages: list[str] | None = None) -> list[dict]:
    """Places in the areas, one row per place at its best (lowest) tier, ranked by tier
    then confidence. At most max_places rows (default OVERTURE_MAX_PLACES; 0 = no cap);
    when more matched, a note goes to `messages`. Country/region filters run in SQL."""
    if not categories or not areas:
        return []
    cap = settings.overture_max_places if max_places is None else max_places
    src = _places_glob(release)
    marks = ",".join("?" * len(categories))
    selects: list[str] = []
    params: list = []
    for a in areas:
        for (xmin, ymin, xmax, ymax), box_rv in zip(a.boxes, a.box_regions or [None] * len(a.boxes)):
            where = [
                "bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?",
                f"basic_category IN ({marks})",
                "operating_status IS DISTINCT FROM 'permanently_closed'",
                "confidence >= ? AND len(websites) > 0",
            ]
            params += [xmin, xmax, ymin, ymax, *categories, min_confidence]
            if a.country_code:
                where.append("(addresses[1].country = ? OR addresses[1].country IS NULL "
                             "OR addresses[1].country = '')")
                params.append(a.country_code)
            for rv in (a.region_values, box_rv):
                if rv:
                    clause, vals = _region_filter(rv)
                    where.append(clause)
                    params += vals
            selects.append(
                f"SELECT {_PLACE_COLUMNS}, {int(a.tier)} AS tier "
                f"FROM read_parquet({_lit(src)}, hive_partitioning=1) WHERE " + " AND ".join(where))
    sql = ("SELECT * FROM (" + " UNION ALL ".join(selects) + ") "
           "QUALIFY row_number() OVER (PARTITION BY id ORDER BY tier) = 1 "
           "ORDER BY tier, confidence DESC, id")
    if cap > 0:
        sql += " LIMIT ?"
        params.append(cap + 1)
    try:
        con = _connect(src)
        try:
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    if 0 < cap < len(rows):
        rows = rows[:cap]
        if messages is not None:
            messages.append(_cap_message(cap))
    return [{
        "source_id": pid, "name": name, "category": cat, "website_url": site,
        "emails": list(emails or []), "phones": list(phones or []), "brand": brand,
        "confidence": conf, "city": city, "region": region, "country": country,
        "lat": lat, "lon": lon, "tier": tier,
    } for (pid, name, cat, site, emails, phones, brand, conf, city, region, country,
           lat, lon, tier) in rows]
