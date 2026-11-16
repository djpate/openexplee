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
    region_values: set[str] | None
    country_code: str | None


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
        "SELECT bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax, country "
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


def resolve_areas(specs: list[AreaSpec], release: str) -> tuple[list[ResolvedArea], list[str]]:
    src = _divisions_glob(release)
    areas: list[ResolvedArea] = []
    missing: list[str] = []
    try:
        con = _connect(src)
        try:
            for s in specs:
                rows = _lookup(con, src, s.name, SUBTYPES.get(s.kind, (s.kind,)),
                               s.region_code, s.country_code)
                if not rows:
                    missing.append(s.name)
                    continue
                country = rows[0][4] or s.country_code
                region_values = None
                if s.kind == "country":
                    regions = con.execute(
                        "SELECT bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax "
                        f"FROM read_parquet({_lit(src)}, hive_partitioning=1) "
                        "WHERE subtype = 'region' AND country = ?", [country]).fetchall()
                    boxes = sorted({tuple(r) for r in regions}) or [_union(rows)]
                elif s.kind == "region":
                    region_values = {s.name}
                    if s.region_code and "-" in s.region_code:
                        region_values.add(s.region_code.split("-", 1)[1])
                    boxes = [_union(rows)]
                else:
                    boxes = [_union(rows)]
                areas.append(ResolvedArea(s.name, s.kind, s.tier, boxes, region_values,
                                          country if s.kind == "country" else s.country_code))
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    return areas, missing


def fetch_places(release: str, categories: list[str], areas: list[ResolvedArea],
                 min_confidence: float) -> list[dict]:
    if not categories or not areas:
        return []
    src = _places_glob(release)
    marks = ",".join("?" * len(categories))
    selects: list[str] = []
    params: list = []
    for i, a in enumerate(areas):
        for xmin, ymin, xmax, ymax in a.boxes:
            selects.append(
                "SELECT id, names.\"primary\" AS name, basic_category, websites[1] AS website, "
                "emails, phones, brand.names.\"primary\" AS brand, confidence, "
                "addresses[1].locality AS city, addresses[1].region AS region, "
                "addresses[1].country AS country, bbox.ymin AS lat, bbox.xmin AS lon, "
                f"{i} AS area_idx FROM read_parquet({_lit(src)}, hive_partitioning=1) "
                "WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ? "
                f"AND basic_category IN ({marks}) "
                "AND operating_status IS DISTINCT FROM 'permanently_closed' "
                "AND confidence >= ? AND len(websites) > 0"
            )
            params += [xmin, xmax, ymin, ymax, *categories, min_confidence]
    try:
        con = _connect(src)
        try:
            rows = con.execute(" UNION ALL ".join(selects), params).fetchall()
        finally:
            con.close()
    except duckdb.Error as e:
        raise OvertureError(str(e)) from e
    best: dict[str, dict] = {}
    for (pid, name, cat, site, emails, phones, brand, conf, city, region, country,
         lat, lon, idx) in rows:
        a = areas[idx]
        if a.region_values is not None and region and region not in a.region_values:
            continue
        if a.country_code and country and country != a.country_code:
            continue
        cur = best.get(pid)
        if cur and cur["tier"] <= a.tier:
            continue
        best[pid] = {
            "source_id": pid, "name": name, "category": cat, "website_url": site,
            "emails": list(emails or []), "phones": list(phones or []), "brand": brand,
            "confidence": conf, "city": city, "region": region, "country": country,
            "lat": lat, "lon": lon, "tier": a.tier,
        }
    return sorted(best.values(), key=lambda r: (r["tier"], r["source_id"]))
