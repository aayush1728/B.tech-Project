"""
load_data.py

Migrates the static qgis2web-exported layers (baked into JS variable
assignments like `var json_ContourMap_1 = {...GeoJSON...}`) into real,
queryable PostGIS tables -- georeferenced to true WGS84 on the way in.

The legacy exports claim CRS84 but actually hold *pixel* coordinates of an
un-georeferenced scan (see georeference.py and the README "CRS postmortem").
Every vertex goes through georeference.legacy_to_wgs84() before loading.

Boundary layers were digitised as open polylines whose ends miss by
0.2-4 m; they are closed and rebuilt as polygons so areas, overlaps and
containment queries work.

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
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping, shape
from shapely.ops import linemerge, unary_union

from georeference import georeference_geojson, report

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SOURCE_DIR = Path(__file__).resolve().parent / "source_layers"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://webgis:webgis_dev_password@localhost:5432/mine_monitoring",
)

# Maps each source file to a target table. `as_polygon` closes digitised
# boundary polylines into polygons.
LAYER_CONFIG = {
    "ContourMap_1.js": {
        "table": "contours", "title": "Elevation contours",
        "description": "Elevation contour lines (m)", "as_polygon": False,
    },
    "LeaseBoundaryMap_2.js": {
        "table": "lease_boundary", "title": "Lease boundary",
        "description": "Mining lease boundary", "as_polygon": True,
    },
    "LigniteBoundaryMap_3.js": {
        "table": "lignite_boundary", "title": "Lignite boundary",
        "description": "Lignite deposit boundary", "as_polygon": True,
    },
    "UltimatePitLimit_4.js": {
        "table": "ultimate_pit_limit", "title": "Ultimate pit limit",
        "description": "Ultimate (final) pit extent boundary", "as_polygon": True,
    },
    "Lakes_5.js": {
        "table": "lakes", "title": "Lakes",
        "description": "Nearby lakes / water bodies", "as_polygon": False,
    },
    "Canals_6.js": {
        "table": "canals", "title": "Canals",
        "description": "Nearby canals", "as_polygon": False,
    },
}

VAR_ASSIGNMENT_RE = re.compile(r"^\s*var\s+\w+\s*=\s*", re.DOTALL)
MAX_CLOSE_GAP_DEG = 0.0002  # ~20 m; refuse to "close" anything wider


def parse_qgis2web_js(path: Path) -> dict:
    """Strip the leading `var json_XXX = ` (and any trailing semicolon)
    from a qgis2web-exported .js file and parse the remaining GeoJSON."""
    raw = path.read_text(encoding="utf-8").strip()
    raw = VAR_ASSIGNMENT_RE.sub("", raw)
    raw = raw.rstrip(";").strip()
    return json.loads(raw)


def close_to_polygon(geom_json: dict) -> dict:
    """Turn a (nearly) closed polyline into a valid (Multi)Polygon."""
    geom = shape(geom_json)
    if geom.geom_type == "MultiLineString" and len(geom.geoms) > 1:
        geom = linemerge(geom)
    lines = list(getattr(geom, "geoms", [geom]))
    polys = []
    for ln in lines:
        coords = list(ln.coords)
        gap = LineString([coords[0], coords[-1]]).length
        if gap > MAX_CLOSE_GAP_DEG:
            raise ValueError(f"ring gap {gap:.6f} deg too large to close safely")
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        polys.append(poly)
    out = unary_union(polys)
    if isinstance(out, Polygon):
        out = MultiPolygon([out])
    return mapping(out)


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


def insert_features(cur, table: str, geojson: dict, as_polygon: bool):
    rows = []
    for f in geojson["features"]:
        g = f.get("geometry")
        if g is None:
            continue
        if as_polygon:
            g = close_to_polygon(g)
        rows.append((json.dumps(f.get("properties") or {}), json.dumps(g)))
    if not rows:
        return 0
    execute_values(
        cur,
        f"INSERT INTO {table} (properties, geom) VALUES %s",
        rows,
        template="(%s::jsonb, ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))",
    )
    return len(rows)


def create_layer_registry(cur):
    cur.execute("DROP TABLE IF EXISTS layers;")
    cur.execute(
        """
        CREATE TABLE layers (
            name TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            sort_order INT NOT NULL
        );
        """
    )
    execute_values(
        cur,
        "INSERT INTO layers (name, title, description, sort_order) VALUES %s",
        [(c["table"], c["title"], c["description"], i)
         for i, c in enumerate(LAYER_CONFIG.values())],
    )


def main():
    print("Georeferencing transform:\n" + report() + "\n")
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

            geojson = georeference_geojson(parse_qgis2web_js(src))
            feature_count = len(geojson.get("features", []))

            create_table(cur, cfg["table"])
            inserted = insert_features(cur, cfg["table"], geojson, cfg["as_polygon"])
            conn.commit()

            total += inserted
            kind = "polygon" if cfg["as_polygon"] else "as-is"
            print(f"  [ok] {filename} -> {cfg['table']}: "
                  f"{inserted}/{feature_count} features ({kind})")

        create_layer_registry(cur)
        conn.commit()

        cur.execute(
            "SELECT round((ST_Area(geom::geography) / 1e4)::numeric, 1) FROM lease_boundary"
        )
        lease_ha = cur.fetchone()[0]
        print(f"\nDone. {total} total features loaded across {len(LAYER_CONFIG)} layers.")
        print(f"Sanity check: lease area = {lease_ha} ha (published: ~3,672 ha)")

    except Exception as exc:
        conn.rollback()
        print(f"Migration failed, rolled back: {exc}", file=sys.stderr)
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
