"""
load_data.py

Migrates the static qgis2web-exported layers (baked into JS variable
assignments like `var json_ContourMap_1 = {...GeoJSON...}`) into real,
queryable PostGIS tables.

Source CRS in the exports is CRS84 (== WGS84 / EPSG:4326), so geometries
are loaded as-is with SRID 4326, no reprojection needed.

Usage:
    1. Start PostGIS:      docker compose up -d postgis
    2. Install deps:       pip install -r requirements.txt
    3. Run migration:      python load_data.py
"""

import json
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SOURCE_DIR = Path(__file__).resolve().parent / "source_layers"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://webgis:webgis_dev_password@localhost:5432/mine_monitoring",
)

# Maps each source file to a target table name and a human-readable
# description. Geometry type is inferred at load time from the GeoJSON
# itself, so it doesn't need to be hardcoded here.
LAYER_CONFIG = {
    "ContourMap_1.js": {
        "table": "contours",
        "description": "Elevation contour lines",
    },
    "LeaseBoundaryMap_2.js": {
        "table": "lease_boundary",
        "description": "Mining lease boundary",
    },
    "LigniteBoundaryMap_3.js": {
        "table": "lignite_boundary",
        "description": "Lignite deposit boundary",
    },
    "UltimatePitLimit_4.js": {
        "table": "ultimate_pit_limit",
        "description": "Ultimate (final) pit extent boundary",
    },
    "Lakes_5.js": {
        "table": "lakes",
        "description": "Nearby lakes / water bodies",
    },
    "Canals_6.js": {
        "table": "canals",
        "description": "Nearby canals",
    },
}

VAR_ASSIGNMENT_RE = re.compile(r"^\s*var\s+\w+\s*=\s*", re.DOTALL)


def parse_qgis2web_js(path: Path) -> dict:
    """Strip the leading `var json_XXX = ` (and any trailing semicolon)
    from a qgis2web-exported .js file and parse the remaining GeoJSON."""
    raw = path.read_text(encoding="utf-8").strip()
    raw = VAR_ASSIGNMENT_RE.sub("", raw)
    raw = raw.rstrip(";").strip()
    return json.loads(raw)


def geometry_types_in(geojson: dict) -> set:
    return {f["geometry"]["type"] for f in geojson["features"] if f.get("geometry")}


def create_table(cur, table: str):
    cur.execute(f"DROP TABLE IF EXISTS {table};")
    cur.execute(
        f"""
        CREATE TABLE {table} (
            id SERIAL PRIMARY KEY,
            properties JSONB,
            geom GEOMETRY(Geometry, 4326)
        );
        """
    )
    cur.execute(f"CREATE INDEX {table}_geom_idx ON {table} USING GIST (geom);")


def insert_features(cur, table: str, geojson: dict):
    rows = [
        (json.dumps(f.get("properties") or {}), json.dumps(f["geometry"]))
        for f in geojson["features"]
        if f.get("geometry") is not None
    ]
    if not rows:
        return 0
    execute_values(
        cur,
        f"""
        INSERT INTO {table} (properties, geom)
        VALUES %s
        """,
        rows,
        template="(%s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))",
    )
    return len(rows)


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        conn.commit()

        total = 0
        for filename, cfg in LAYER_CONFIG.items():
            src = SOURCE_DIR / filename
            if not src.exists():
                print(f"  [skip] {filename} not found in source_layers/")
                continue

            geojson = parse_qgis2web_js(src)
            feature_count = len(geojson.get("features", []))
            geom_types = geometry_types_in(geojson)

            create_table(cur, cfg["table"])
            inserted = insert_features(cur, cfg["table"], geojson)
            conn.commit()

            total += inserted
            print(
                f"  [ok] {filename} -> {cfg['table']}: "
                f"{inserted}/{feature_count} features loaded "
                f"(geometry types: {', '.join(sorted(geom_types))})"
            )

        print(f"\nDone. {total} total features loaded across "
              f"{len(LAYER_CONFIG)} layers.")

    except Exception as exc:
        conn.rollback()
        print(f"Migration failed, rolled back: {exc}", file=sys.stderr)
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
