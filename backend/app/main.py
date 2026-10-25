"""
Mine Site Monitoring API -- Surkha (North) Lignite Mine.

Serves the six georeferenced survey layers from PostGIS as GeoJSON and
computes monitoring statistics directly in the database:

    GET  /health
    GET  /api/layers                      layer catalogue (counts, types, extents)
    GET  /api/layers/{name}               a layer as GeoJSON (?bbox=, ?simplify_m=)
    GET  /api/stats/summary               areas, lease compliance, elevation range
    GET  /api/analysis/water-setback      lakes/canals within N m of the pit limit
    GET  /api/analysis/lignite-outside-lease   deposit lying outside the lease
    GET  /api/analysis/point              "what is here?" for a clicked location

Spatial conventions: geometries are stored in EPSG:4326. Areas and distances
use `::geography` (true metres on the WGS84 ellipsoid), never planar degrees
or Web Mercator.
"""

import datetime as dt
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db

# Whitelist: table names can't be bound as SQL parameters, so every layer
# name is checked against this set before it is interpolated.
LAYERS = {
    "contours", "lease_boundary", "lignite_boundary",
    "ultimate_pit_limit", "lakes", "canals",
}
WATER_LAYERS = ("lakes", "canals")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    db.close_pool()


app = FastAPI(
    title="Mine Site Monitoring API",
    description="PostGIS-backed monitoring API for the Surkha (North) Lignite Mine.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
)


def _check_layer(name: str) -> str:
    if name not in LAYERS:
        raise HTTPException(404, f"Unknown layer '{name}'. Try one of: {sorted(LAYERS)}")
    return name


def _parse_bbox(bbox: str | None):
    if bbox is None:
        return None
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must be 'minLon,minLat,maxLon,maxLat'") from None
    if not (minx < maxx and miny < maxy):
        raise HTTPException(422, "bbox min values must be smaller than max values")
    return minx, miny, maxx, maxy


@app.get("/health")
def health():
    row = db.fetch_one("SELECT PostGIS_Lib_Version() AS postgis")
    # RENDER_GIT_COMMIT is set by Render; CI polls it to know the new build is live
    return {"status": "ok", "postgis": row["postgis"],
            "commit": os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "local"))}


@app.get("/api/layers")
def list_layers():
    meta = {r["name"]: r for r in db.fetch_all(
        "SELECT name, title, description, sort_order FROM layers")}
    out = []
    for name in sorted(LAYERS, key=lambda n: meta.get(n, {}).get("sort_order", 99)):
        row = db.fetch_one(
            f"""
            SELECT count(*) AS feature_count,
                   array_agg(DISTINCT GeometryType(geom)) AS geometry_types,
                   ST_XMin(ST_Extent(geom)) AS minx, ST_YMin(ST_Extent(geom)) AS miny,
                   ST_XMax(ST_Extent(geom)) AS maxx, ST_YMax(ST_Extent(geom)) AS maxy
            FROM {name}
            """
        )
        m = meta.get(name, {})
        out.append({
            "name": name,
            "title": m.get("title", name),
            "description": m.get("description"),
            "feature_count": row["feature_count"],
            "geometry_types": row["geometry_types"],
            "bbox": [row["minx"], row["miny"], row["maxx"], row["maxy"]],
        })
    return out


@app.get("/api/layers/{name}")
def get_layer(
    name: str,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat (EPSG:4326)"),
    simplify_m: float = Query(0, ge=0, le=100, description="Douglas-Peucker tolerance, metres"),
):
    name = _check_layer(name)
    box = _parse_bbox(bbox)
    where, params = "", {"tol_deg": simplify_m / 111_320.0}
    if box:
        where = "WHERE geom && ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326)"
        params.update(dict(zip(("minx", "miny", "maxx", "maxy"), box, strict=True)))
    row = db.fetch_one(
        f"""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'name', %(name)s,
            'features', coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'id', id,
                'properties', properties || jsonb_build_object('id', id),
                'geometry', ST_AsGeoJSON(
                    CASE WHEN %(tol_deg)s > 0
                         THEN ST_SimplifyPreserveTopology(geom, %(tol_deg)s)
                         ELSE geom END, 7)::json
            ) ORDER BY id), '[]'::json)
        ) AS fc
        FROM {name} {where}
        """,
        {**params, "name": name},
    )
    return JSONResponse(row["fc"], media_type="application/geo+json")


@app.get("/api/stats/summary")
def summary():
    return db.fetch_one(
        """
        WITH l AS (SELECT ST_Union(geom) g FROM lease_boundary),
             d AS (SELECT ST_Union(geom) g FROM lignite_boundary),
             p AS (SELECT ST_Union(geom) g FROM ultimate_pit_limit)
        SELECT
          round((ST_Area(l.g::geography) / 1e4)::numeric, 1)   AS lease_area_ha,
          round((ST_Area(d.g::geography) / 1e4)::numeric, 1)   AS lignite_area_ha,
          round((ST_Area(p.g::geography) / 1e4)::numeric, 1)   AS pit_area_ha,
          round((ST_Perimeter(l.g::geography) / 1e3)::numeric, 2) AS lease_perimeter_km,
          round((100 * ST_Area(p.g::geography) / ST_Area(l.g::geography))::numeric, 1)
                                                               AS pit_pct_of_lease,
          round((100 * ST_Area(ST_Intersection(d.g, p.g)::geography)
                     / ST_Area(d.g::geography))::numeric, 1)   AS lignite_pct_within_pit,
          round((ST_Area(ST_Difference(d.g, l.g)::geography) / 1e4)::numeric, 1)
                                                               AS lignite_outside_lease_ha,
          round((ST_Area(ST_Difference(p.g, l.g)::geography) / 1e4)::numeric, 2)
                                                               AS pit_outside_lease_ha,
          (SELECT min((properties->>'Value')::numeric) FROM contours) AS min_contour_m,
          (SELECT max((properties->>'Value')::numeric) FROM contours) AS max_contour_m,
          ST_X(ST_Centroid(l.g)) AS centroid_lon,
          ST_Y(ST_Centroid(l.g)) AS centroid_lat
        FROM l, d, p
        """
    )


@app.get("/api/analysis/water-setback")
def water_setback(buffer_m: float = Query(100, gt=0, le=5000)):
    """Water features (lakes, canals) closer than `buffer_m` to the ultimate
    pit limit -- the kind of setback check environmental clearances impose."""
    rows = db.fetch_all(
        f"""
        WITH p AS (SELECT ST_Union(geom) g FROM ultimate_pit_limit),
        w AS (
            {" UNION ALL ".join(
                f"SELECT '{t}' AS layer, id, properties, geom FROM {t}" for t in WATER_LAYERS)}
        )
        SELECT w.layer, w.id, w.properties->>'Class' AS class,
               round(ST_Distance(w.geom::geography, p.g::geography)::numeric, 1) AS distance_m,
               ST_Intersects(w.geom, p.g) AS inside_pit,
               ST_AsGeoJSON(w.geom, 7)::json AS geometry
        FROM w, p
        WHERE ST_DWithin(w.geom::geography, p.g::geography, %(buf)s)
        ORDER BY distance_m
        """,
        {"buf": buffer_m},
    )
    return {
        "buffer_m": buffer_m,
        "violations": len(rows),
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": r.pop("geometry"), "properties": r} for r in rows
        ],
    }


@app.get("/api/analysis/lignite-outside-lease")
def lignite_outside_lease():
    row = db.fetch_one(
        """
        WITH g AS (
            SELECT (ST_Dump(ST_Difference(
                (SELECT ST_Union(geom) FROM lignite_boundary),
                (SELECT ST_Union(geom) FROM lease_boundary)))).geom AS geom
        )
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'properties', json_build_object(
                    'area_ha', round((ST_Area(geom::geography) / 1e4)::numeric, 2)),
                'geometry', ST_AsGeoJSON(geom, 7)::json)), '[]'::json)) AS fc
        FROM g WHERE ST_Area(geom::geography) > 1
        """
    )
    return JSONResponse(row["fc"], media_type="application/geo+json")


@app.get("/api/analysis/point")
def point_info(
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
):
    """Everything the map needs to answer 'what is at this spot?'."""
    pt = "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)"
    p = {"lon": lon, "lat": lat}
    zones = db.fetch_one(
        f"""
        SELECT
          EXISTS (SELECT 1 FROM lease_boundary     WHERE ST_Intersects(geom, {pt})) AS in_lease,
          EXISTS (SELECT 1 FROM lignite_boundary   WHERE ST_Intersects(geom, {pt})) AS in_lignite,
          EXISTS (SELECT 1 FROM ultimate_pit_limit WHERE ST_Intersects(geom, {pt})) AS in_pit
        """, p)
    nearest_water = db.fetch_one(
        f"""
        SELECT layer, round(d::numeric, 1) AS distance_m FROM (
            SELECT 'lake' AS layer, ST_Distance(geom::geography, {pt}::geography) d FROM lakes
            UNION ALL
            SELECT 'canal', ST_Distance(geom::geography, {pt}::geography) FROM canals
        ) s ORDER BY d LIMIT 1
        """, p)
    # nearest contour via KNN index operator, then exact distance
    contour = db.fetch_one(
        f"""
        SELECT (properties->>'Value')::numeric AS elevation_m,
               round(ST_Distance(geom::geography, {pt}::geography)::numeric, 1) AS distance_m
        FROM contours ORDER BY geom <-> {pt} LIMIT 1
        """, p)
    boundary = db.fetch_one(
        f"""
        SELECT round(ST_Distance(ST_Boundary(geom)::geography, {pt}::geography)::numeric, 1)
               AS distance_m FROM lease_boundary LIMIT 1
        """, p)
    return {
        "lon": lon, "lat": lat, **zones,
        "nearest_water": nearest_water,
        "nearest_contour": contour,
        "distance_to_lease_boundary_m": boundary["distance_m"] if boundary else None,
    }


# --------------------------------------------------------------------------
# Sentinel-2 monitoring (tables filled by monitoring/s2_monitor.py)
# --------------------------------------------------------------------------
def _monitoring_ready() -> bool:
    row = db.fetch_one("SELECT to_regclass('s2_observations') IS NOT NULL AS ok")
    return bool(row and row["ok"])


def _nearest_obs(date: str) -> str | None:
    """Snap a requested date to the nearest stored observation date."""
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "dates must be YYYY-MM-DD") from None
    row = db.fetch_one(
        "SELECT obs_date FROM s2_observations ORDER BY abs(obs_date - %(d)s::date), obs_date LIMIT 1",
        {"d": d})
    return row["obs_date"].isoformat() if row else None


@app.get("/api/monitoring/timeseries")
def monitoring_timeseries():
    """One row per month: areas (ha) of bare ground, water and vegetation by zone."""
    if not _monitoring_ready():
        return {"count": 0, "observations": []}
    rows = db.fetch_all(
        """
        SELECT obs_date::text, scene_id, scene_cloud_pct, valid_pct,
               round(lease_bare_ha::numeric, 1)::float AS lease_bare_ha,
               round(lease_water_ha::numeric, 1)::float AS lease_water_ha,
               round(lease_veg_ha::numeric, 1)::float AS lease_veg_ha,
               round(pit_bare_ha::numeric, 1)::float AS pit_bare_ha,
               round(pit_water_ha::numeric, 1)::float AS pit_water_ha,
               round(outside_pit_bare_ha::numeric, 1)::float AS outside_pit_bare_ha,
               round(buffer_bare_ha::numeric, 1)::float AS buffer_bare_ha
        FROM s2_observations ORDER BY obs_date
        """)
    return {"count": len(rows), "observations": rows}


@app.get("/api/monitoring/footprint")
def monitoring_footprint(date: str = Query(..., description="YYYY-MM-DD; snapped to the nearest observation")):
    if not _monitoring_ready() or not (d := _nearest_obs(date)):
        raise HTTPException(404, "no Sentinel-2 observations stored yet")
    row = db.fetch_one(
        """
        SELECT json_build_object('type', 'FeatureCollection', 'obs_date', %(d)s,
          'features', coalesce(json_agg(json_build_object('type', 'Feature',
             'properties', json_build_object('class', class, 'area_ha', round(area_ha::numeric, 1), 'obs_date', obs_date),
             'geometry', ST_AsGeoJSON(geom, 6)::json)), '[]'::json)) AS fc
        FROM s2_footprint WHERE obs_date = %(d)s::date
        """, {"d": d})
    return JSONResponse(row["fc"], media_type="application/geo+json")


@app.get("/api/monitoring/change")
def monitoring_change(
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """Ground that became bare (newly excavated / dumped) or stopped being bare
    (reclaimed / flooded) between two observations."""
    if not _monitoring_ready():
        raise HTTPException(404, "no Sentinel-2 observations stored yet")
    a, b = _nearest_obs(date_from), _nearest_obs(date_to)
    if not a or not b or a == b:
        raise HTTPException(422, "need two different observation dates")
    row = db.fetch_one(
        """
        WITH f AS (SELECT coalesce((SELECT geom FROM s2_footprint WHERE obs_date = %(a)s::date AND class = 'bare'),
                                   'MULTIPOLYGON EMPTY'::geometry) g),
             t AS (SELECT coalesce((SELECT geom FROM s2_footprint WHERE obs_date = %(b)s::date AND class = 'bare'),
                                   'MULTIPOLYGON EMPTY'::geometry) g),
             pit AS (SELECT ST_Union(geom) g FROM ultimate_pit_limit),
             parts AS (
               SELECT 'new_bare' AS kind, (ST_Dump(ST_Difference(ST_SetSRID(t.g, 4326), ST_SetSRID(f.g, 4326)))).geom FROM f, t
               UNION ALL
               SELECT 'no_longer_bare', (ST_Dump(ST_Difference(ST_SetSRID(f.g, 4326), ST_SetSRID(t.g, 4326)))).geom FROM f, t),
             big AS (SELECT kind, geom, ST_Area(geom::geography) / 1e4 AS ha,
                            ST_Area(ST_Difference(geom, pit.g)::geography) / 1e4 AS ha_outside_pit
                     FROM parts, pit WHERE ST_Area(geom::geography) > 5000)
        SELECT
          coalesce(round(sum(ha) FILTER (WHERE kind = 'new_bare')::numeric, 1), 0)::float AS new_bare_ha,
          coalesce(round(sum(ha_outside_pit) FILTER (WHERE kind = 'new_bare')::numeric, 1), 0)::float
              AS new_bare_outside_pit_ha,
          coalesce(round(sum(ha) FILTER (WHERE kind = 'no_longer_bare')::numeric, 1), 0)::float AS no_longer_bare_ha,
          coalesce(json_agg(json_build_object('type', 'Feature',
              'properties', json_build_object('kind', kind, 'area_ha', round(ha::numeric, 2)),
              'geometry', ST_AsGeoJSON(geom, 6)::json)) FILTER (WHERE kind IS NOT NULL), '[]'::json) AS features
        FROM big
        """, {"a": a, "b": b})
    return {"type": "FeatureCollection", "from": a, "to": b, **row}


@app.get("/api/monitoring/alerts")
def monitoring_alerts(persist: int = Query(6, ge=2, le=24, description="consecutive observations")):
    """Bare ground that stays bare across the last `persist` observations, split into
    inside-lease-but-outside-the-ultimate-pit and outside-the-lease. Persistence is
    what separates excavation or dumping from a field left fallow for a season."""
    if not _monitoring_ready():
        return {"persist": persist, "dates": [], "features": []}
    dates = [r["obs_date"].isoformat() for r in db.fetch_all(
        "SELECT obs_date FROM s2_observations ORDER BY obs_date DESC LIMIT %(n)s", {"n": persist})]
    if len(dates) < persist:
        return {"persist": persist, "dates": dates, "features": [],
                "note": f"need {persist} observations, have {len(dates)}"}
    row = db.fetch_one(
        """
        WITH RECURSIVE f AS (
            SELECT row_number() OVER (ORDER BY obs_date DESC) AS rn, geom
            FROM s2_footprint WHERE class = 'bare' AND obs_date = ANY(%(dates)s::date[])),
        r(rn, g) AS (
            SELECT rn, geom::geometry FROM f WHERE rn = 1
            UNION ALL
            SELECT f.rn, ST_Intersection(r.g, f.geom) FROM r JOIN f ON f.rn = r.rn + 1),
        persistent AS (SELECT g FROM r ORDER BY rn DESC LIMIT 1),
        z AS (SELECT (SELECT ST_Union(geom) FROM lease_boundary) lease,
                     (SELECT ST_Union(geom) FROM ultimate_pit_limit) pit),
        parts AS (
            SELECT 'inside_lease_outside_pit' AS zone,
                   (ST_Dump(ST_Difference(ST_Intersection(p.g, z.lease), z.pit))).geom FROM persistent p, z
            UNION ALL
            SELECT 'outside_lease', (ST_Dump(ST_Difference(p.g, z.lease))).geom FROM persistent p, z)
        SELECT (SELECT count(*) FROM f) AS n_footprints,
               coalesce(json_agg(json_build_object('type', 'Feature',
                 'properties', json_build_object('zone', zone,
                     'area_ha', round((ST_Area(geom::geography) / 1e4)::numeric, 2)),
                 'geometry', ST_AsGeoJSON(geom, 6)::json))
                 FILTER (WHERE ST_Area(geom::geography) > 10000), '[]'::json) AS features
        FROM parts
        """, {"dates": dates})
    if row["n_footprints"] < persist:
        return {"persist": persist, "dates": dates, "features": [],
                "note": "some observations have no bare footprint"}
    return {"persist": persist, "dates": dates, "type": "FeatureCollection", "features": row["features"]}


# Serve the Leaflet frontend from the same origin: FRONTEND_DIR in Docker,
# otherwise the repo's ./frontend folder during local development.
_frontend = Path(os.getenv("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
