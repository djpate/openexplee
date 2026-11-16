"""Local parquet fixtures in Overture's nested shape (no network)."""
import json

import duckdb
import pytest

BBOX = "STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE)"
ADDRESS = "STRUCT(freeform VARCHAR, locality VARCHAR, postcode VARCHAR, region VARCHAR, country VARCHAR)[]"
PLACES_COLUMNS = {
    "id": "VARCHAR",
    "names": 'STRUCT("primary" VARCHAR)',
    "basic_category": "VARCHAR",
    "taxonomy": 'STRUCT("primary" VARCHAR)',
    "websites": "VARCHAR[]",
    "emails": "VARCHAR[]",
    "phones": "VARCHAR[]",
    "brand": 'STRUCT(names STRUCT("primary" VARCHAR))',
    "confidence": "DOUBLE",
    "operating_status": "VARCHAR",
    "addresses": ADDRESS,
    "bbox": BBOX,
}
DIVISIONS_COLUMNS = {
    "id": "VARCHAR",
    "names": 'STRUCT("primary" VARCHAR)',
    "subtype": "VARCHAR",
    "country": "VARCHAR",
    "region": "VARCHAR",
    "bbox": BBOX,
}


def _write(path, rows, columns):
    src = f"{path}.json"
    with open(src, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    struct = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_json('{src}', format='newline_delimited', columns={struct})) "
            f"TO '{path}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def write_places(path, rows):
    _write(str(path), rows, PLACES_COLUMNS)


def write_divisions(path, rows):
    _write(str(path), rows, DIVISIONS_COLUMNS)


def place(**o):
    """Default open Tampa dental clinic. Convenience keys: name, category, lon, lat,
    locality, region, country; any other key overrides the column directly."""
    lon, lat = o.pop("lon", -82.46), o.pop("lat", 27.95)
    name, cat = o.pop("name", "Tampa Dental"), o.pop("category", "dental_clinic")
    addr = {
        "freeform": "1 Main St",
        "locality": o.pop("locality", "Tampa"),
        "postcode": "33601",
        "region": o.pop("region", "FL"),
        "country": o.pop("country", "US"),
    }
    row = {
        "id": "p1",
        "names": {"primary": name},
        "basic_category": cat,
        "taxonomy": {"primary": cat},
        "websites": ["https://tampadental.com"],
        "emails": ["info@tampadental.com"],
        "phones": ["+18135550100"],
        "brand": None,
        "confidence": 0.9,
        "operating_status": "open",
        "addresses": [addr],
        "bbox": {"xmin": lon, "xmax": lon, "ymin": lat, "ymax": lat},
    }
    row.update(o)
    return row


def division(**o):
    """Default Tampa locality. Convenience keys: name, box=(xmin, ymin, xmax, ymax)."""
    xmin, ymin, xmax, ymax = o.pop("box", (-82.6, 27.85, -82.25, 28.17))
    row = {
        "id": "d1",
        "names": {"primary": o.pop("name", "Tampa")},
        "subtype": "locality",
        "country": "US",
        "region": "US-FL",
        "bbox": {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax},
    }
    row.update(o)
    return row


@pytest.fixture
def overture_files(tmp_path, monkeypatch):
    from app.config import settings

    places, divs = tmp_path / "places.parquet", tmp_path / "divisions.parquet"
    monkeypatch.setattr(settings, "overture_places_source", str(places))
    monkeypatch.setattr(settings, "overture_divisions_source", str(divs))
    monkeypatch.setattr(settings, "overture_release", "")
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cache" / "t.db"))
    return write_places, write_divisions
